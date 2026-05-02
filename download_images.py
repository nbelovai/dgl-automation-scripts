#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Article Image Downloader (v2.4 - curl_cffi edition).
- Uses curl_cffi to bypass Cloudflare and other bot protection
- Based on v2.3 with all original features preserved
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from html import unescape
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, unquote

from curl_cffi import requests
from curl_cffi.requests import exceptions as curl_exceptions
from bs4 import BeautifulSoup
from PIL import Image

if sys.version_info < (3, 8):
    raise RuntimeError("Python 3.8+ is required to run this script.")


class ArticleImageDownloader:
    """
    Загрузчик изображений из веб-статей с умной фильтрацией.

    Возможности:
    - Поиск контентных изображений (игнорирует рекламу, аватары, UI-иконки)
    - Обложка (OG/Twitter и из DOM), галереи (JSON/PhoneArena), Juxtapose
    - Полноразмерные GSM Arena и поддержка пагинации Tom's Hardware
    - Валидация изображений и ограничение размера
    - Обход Cloudflare через curl_cffi с impersonate
    """

    # Константы по умолчанию (могут переопределяться из __init__/CLI)
    MAX_FILENAME_LENGTH = 80
    DEFAULT_CHUNK_SIZE = 8192
    MAX_FILE_SIZE_MB = 50     # Максимальный размер файла в МБ
    MAX_PAGES = 20            # Максимальное количество страниц для многостраничных статей
    ALLOWED_SCHEMES = ['http', 'https']

    def __init__(
        self,
        download_dir: str = "downloaded_images",
        min_size: int = 20,
        debug: bool = True,
        pause_between_downloads: float = 0.5,
        max_file_size_mb: Optional[int] = None,
        max_pages: Optional[int] = None,
        hash_dedup: bool = False,
        log_file: Optional[str] = None,
    ):
        self.download_dir = download_dir
        self.min_size = min_size
        self.debug = debug
        self.pause_between_downloads = pause_between_downloads
        self.hash_dedup = hash_dedup
        self._seen_hashes: Set[str] = set()

        # Переопределяем лимиты при необходимости
        if max_file_size_mb is not None:
            self.MAX_FILE_SIZE_MB = max_file_size_mb
        if max_pages is not None:
            self.MAX_PAGES = max_pages

        # Логирование
        self._setup_logging(log_file)

        # HTTP-сессия с curl_cffi (impersonate Chrome для обхода Cloudflare)
        self.session = requests.Session(impersonate="chrome")

        # Папка для загрузки
        os.makedirs(download_dir, exist_ok=True)
        self._folder_counter = self._get_next_folder_number()

    # ---------- ЛОГИ ----------
    def _setup_logging(self, log_file: Optional[str] = None) -> None:
        level = logging.DEBUG if self.debug else logging.INFO
        self.logger = logging.getLogger("article_image_downloader")
        self.logger.setLevel(level)

        # Сбрасываем предыдущие хендлеры, чтобы избежать дублирования
        if self.logger.handlers:
            for h in list(self.logger.handlers):
                self.logger.removeHandler(h)

        fmt = logging.Formatter('%(levelname)s: %(message)s')

        sh = logging.StreamHandler()
        sh.setLevel(level)
        sh.setFormatter(fmt)
        self.logger.addHandler(sh)

        if log_file:
            fh = logging.FileHandler(log_file, encoding='utf-8')
            fh.setLevel(level)
            fh.setFormatter(fmt)
            self.logger.addHandler(fh)

    # ---------- ПАПКИ ----------
    def _get_existing_dirs(self) -> List[str]:
        try:
            return [
                d for d in os.listdir(self.download_dir)
                if os.path.isdir(os.path.join(self.download_dir, d))
            ]
        except FileNotFoundError:
            os.makedirs(self.download_dir, exist_ok=True)
            return []

    def _extract_folder_number(self, dirname: str) -> Optional[int]:
        m = re.match(r'^(\d+)\.\s?', dirname)
        return int(m.group(1)) if m else None

    def _get_next_folder_number(self) -> int:
        dirs = self._get_existing_dirs()
        numbers = [num for d in dirs if (num := self._extract_folder_number(d)) is not None]
        return max(numbers) if numbers else 0

    def create_numbered_article_dir(self, title: str) -> str:
        safe_title = self.clean_filename(title) if title else 'article'
        n = self._folder_counter + 1
        while True:
            folder_name = f"{n}. {safe_title}"
            path = os.path.join(self.download_dir, folder_name)
            if not os.path.exists(path):
                os.makedirs(path, exist_ok=False)
                self._folder_counter = n
                self.logger.info(f"[OK] Создана папка статьи: {folder_name}")
                return path
            n += 1

    # ---------- ИМЕНА/ВАЛИДАЦИЯ ----------
    def clean_filename(self, filename: str) -> str:
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = filename.replace("'", "").replace('"', "")
        filename = filename.replace('–', '-').replace('—', '-').replace('…', '...')
        filename = filename.strip(' .')
        filename = re.sub(r'\s+', ' ', filename)
        return filename[:self.MAX_FILENAME_LENGTH]

    def validate_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in self.ALLOWED_SCHEMES:
                self.logger.warning(f"[WARN] Небезопасная схема URL: {parsed.scheme}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"[ERROR] Ошибка валидации URL: {e}")
            return False

    def _strip_query_fragment(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))

    # ---------- ПАГИНАЦИЯ ----------
    def _collect_article_pages(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        pages = {self._strip_query_fragment(base_url)}
        try:
            base_parsed = urlparse(base_url)
            base_path = base_parsed.path
            if base_parsed.netloc.endswith('tomshardware.com'):
                pages.update(self._collect_tomshardware_pages(soup, base_url))
                return sorted(pages)
            if '.php' in base_path:
                prefix = base_path.split('.php')[0]
                pattern = re.compile(re.escape(prefix) + r'p\d+\.php')
            else:
                prefix = base_path.rstrip('/')
                pattern = re.compile(re.escape(prefix) + r'/page/\d+')
            for link in soup.select('a[href]'):
                href = link['href']
                if not href:
                    continue
                absolute = urljoin(base_url, href)
                absolute_clean = self._strip_query_fragment(absolute)
                parsed = urlparse(absolute_clean)
                if parsed.netloc != base_parsed.netloc:
                    continue
                path = parsed.path
                if path == base_path:
                    pages.add(absolute_clean)
                else:
                    if pattern.match(path):
                        pages.add(absolute_clean)
        except Exception:
            pass
        return sorted(pages)

    def _collect_tomshardware_pages(self, soup: BeautifulSoup, base_url: str) -> Set[str]:
        pages = set()
        base_clean = self._strip_query_fragment(base_url)
        pages.add(base_clean)

        def add_link(href: Optional[str]) -> None:
            if not href:
                return
            absolute = urljoin(base_url, href)
            absolute_clean = self._strip_query_fragment(absolute)
            pages.add(absolute_clean)

        pagination_selectors = [
            'a[rel="next"]', 'a[rel="prev"]',
            'a.next-page', 'a.pagination__next', 'a.pagination__prev',
            'a[data-page]',
        ]

        for selector in pagination_selectors:
            for link in soup.select(selector):
                add_link(link.get('href'))

        for link_tag in soup.find_all('link'):
            rel = link_tag.get('rel') or []
            if any(value.lower() in {'next', 'prev'} for value in rel):
                add_link(link_tag.get('href'))

        return pages

    # ---------- URL НОРМАЛИЗАЦИЯ/ДЕДУП ----------
    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        url = url.strip()
        url = url.split('#', 1)[0]
        parsed = urlparse(url)
        path = re.sub(r'/+', '/', parsed.path)

        size_keys = {'w', 'h', 'width', 'height', 'size', 'resize', 'quality', 'format', 'webp', 'jpeg', 'crop'}
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        filtered_pairs = [(k, v) for k, v in query_pairs if k.lower() not in size_keys]
        query = urlencode(filtered_pairs, doseq=True)

        normalized = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, '', query, ''))
        normalized = normalized.rstrip('?&')
        return normalized

    def _looks_like_image_url(self, url: str) -> bool:
        if not url:
            return False
        sanitized = url.split('#', 1)[0].split('?', 1)[0].lower()
        image_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.avif', '.heic', '.heif')
        return sanitized.endswith(image_exts)

    def _allow_duplicate_image(self, page_host: str, element, source: Optional[str]) -> bool:
        if source in {'gallery_item', 'hero'}:
            return False
        if not element:
            return False

        block_keywords = {
            'widget', 'promo', 'related', 'recommend', 'trending', 'store', 'shop', 'merchant',
            'logo', 'sponsored', 'newsletter', 'footer', 'header', 'sidebar', 'ads', 'advert'
        }
        inline_keywords = {
            'inline', 'bodycopy', 'article', 'content', 'text', 'post', 'entry', 'review', 'main'
        }

        inline_candidate = False
        ancestor = element
        depth = 0
        while ancestor is not None and depth < 6:
            classes = ancestor.get('class') or []
            if isinstance(classes, str):
                classes = [classes]
            class_str = ' '.join(classes).lower()
            if class_str:
                if any(keyword in class_str for keyword in block_keywords):
                    return False
                if any(keyword in class_str for keyword in inline_keywords):
                    inline_candidate = True
            ancestor = getattr(ancestor, 'parent', None)
            depth += 1

        figure = element if getattr(element, 'name', None) == 'figure' else element.find_parent('figure')
        if figure:
            classes = figure.get('class') or []
            if isinstance(classes, str):
                classes = [classes]
            class_str = ' '.join(classes).lower()
            if any(keyword in class_str for keyword in inline_keywords):
                inline_candidate = True

        return inline_candidate

    def _extract_image_id(self, url: str) -> Optional[str]:
        if not url:
            return None
        m = re.search(r'/([0-9]+)-(?:image|[0-9]+)(?:/|$)', url)
        if m:
            return m.group(1)
        m = re.search(r'/([0-9]+)[\-_][^/]+\.(?:jpg|jpeg|png|webp)$', url)
        if m:
            return m.group(1)
        return None

    def _extract_image_stub(self, url: str) -> Optional[str]:
        if not url:
            return None
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path)
        if not filename:
            return None
        stub = os.path.splitext(filename)[0].lower()
        stub = re.sub(r'-(?:[0-9]{3,}-[0-9]{2,}|[0-9]{3,}x[0-9]{2,})$', '', stub)
        return self._apply_domain_stub_rules(parsed, stub)

    def _apply_domain_stub_rules(self, parsed_url, stub: Optional[str]) -> Optional[str]:
        if not stub:
            return stub
        host = parsed_url.netloc.lower()
        if host.endswith('gsmarena.com'):
            return self._augment_stub_for_gsmarena(parsed_url, stub)
        return stub

    def _augment_stub_for_gsmarena(self, parsed_url, stub: str) -> str:
        segments = [segment for segment in parsed_url.path.split('/') if segment]
        if len(segments) < 2:
            return stub
        folder_hint = None
        size_pattern = re.compile(r'-?\d+(?:x\d+)?(?:w\d+)?$')
        for segment in reversed(segments[:-1]):
            if size_pattern.fullmatch(segment):
                continue
            if segment.lower() in {'images', 'imgroot', 'review', 'reviews'}:
                continue
            folder_hint = segment
            break
        if not folder_hint:
            candidate = segments[-2]
            if size_pattern.fullmatch(candidate) and len(segments) >= 3:
                candidate = segments[-3]
            folder_hint = candidate
        if folder_hint:
            return f"{folder_hint.lower()}_{stub}"
        return stub

    def _should_skip_by_url_pattern(self, img_url: str, page_url: str, element=None) -> Optional[str]:
        if not img_url:
            return "Пустой URL"

        url_lower = img_url.lower()
        page_lower = page_url.lower()
        parsed_url = urlparse(img_url)
        host_lower = parsed_url.netloc.lower()
        path_lower = parsed_url.path.lower()

        ad_patterns = [
            '/announcements/', '/announcement/', '/ads/', '/ad/', '/banner', '/promo', 'affiliate',
            '/static/stores/', '/vv/bigpic/', 'arenaev.com'
        ]
        for pattern in ad_patterns:
            if pattern in url_lower:
                if pattern == '/vv/bigpic/' and element is not None:
                    parent = element.find_parent('p')
                    if parent:
                        parent_classes = parent.get('class') or []
                        if isinstance(parent_classes, str):
                            parent_classes = [parent_classes]
                        if 'image-row' in parent_classes:
                            return None
                return f"Реклама/баннер ({pattern})"

        recommendation_patterns = [
            '/recommended/', '/related/', '/trending/', '/popular/', '/latest/', '/news/', '/article/', '/topics/'
        ]
        if any(pattern in path_lower for pattern in recommendation_patterns):
            if 'gallery/' in path_lower or '/imgroot/' in path_lower or host_lower.endswith('fdn.gsmarena.com'):
                return None
            return "Рекомендованный блок"

        if '/reviews/' in page_lower and '/reviews/' in url_lower:
            try:
                base_slug = page_lower.split('/reviews/')[1].split('/')[0]
                target_slug = url_lower.split('/reviews/')[1].split('/')[0]
                if base_slug.split('_')[0] not in target_slug:
                    return "Из другой статьи (reviews)"
            except IndexError:
                pass

        return None

    # ---------- ЗАГОЛОВОК ----------
    def get_article_title(self, soup: BeautifulSoup) -> str:
        title_tags = ['h1', 'title', '.article-title', '.post-title']
        for tag in title_tags:
            element = soup.select_one(tag) if tag.startswith('.') else soup.find(tag)
            if element:
                title = element.get_text().strip()
                if title:
                    return self.clean_filename(title)
        return "article"

    def get_title_from_meta(self, soup: BeautifulSoup) -> Optional[str]:
        title_selectors = ['meta[property="og:title"]', 'meta[name="twitter:title"]', 'title', 'h1']
        for selector in title_selectors:
            element = soup.select_one(selector)
            if element:
                if selector.startswith('meta'):
                    title = element.get('content', '').strip()
                else:
                    title = element.get_text().strip()
                if title:
                    return self.clean_filename(title)
        return None

    # ---------- ПОИСК ИЗОБРАЖЕНИЙ ----------
    def find_content_images(self, soup: BeautifulSoup, url: str) -> List[Dict]:
        images: List[Dict] = []
        hero_normalized: Optional[str] = None
        hero_ids: Set[str] = set()
        hero_stubs: Set[str] = set()
        page_host = urlparse(url).netloc.lower()
        seen_urls: Set[str] = set()
        seen_image_ids: Set[str] = set()
        seen_stubs: Set[str] = set()
        seen_strict_stubs: Set[str] = set()
        json_stubs: Set[str] = set()
        self.logger.debug("=== ПОИСК ОБЛОЖКИ И КОНТЕНТНЫХ ИЗОБРАЖЕНИЙ ===")

        def register(img: Dict, success_label: str, skip_label: str) -> None:
            url_value = img.get('url')
            if not url_value:
                return

            normalized = self._normalize_url(url_value)
            image_id = img.get('image_id') or self._extract_image_id(url_value)
            stub = self._extract_image_stub(url_value)
            is_benchmark = '/benchmarks/' in normalized
            source = img.get('source')
            host = urlparse(normalized).netloc.lower()
            element = img.get('element')
            allow_duplicate = self._allow_duplicate_image(page_host, element, source)

            if hero_normalized and normalized == hero_normalized:
                allow_duplicate = False
            if image_id and image_id in hero_ids:
                allow_duplicate = False
            if stub and stub in hero_stubs:
                allow_duplicate = False

            if normalized in seen_urls and not allow_duplicate:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            if image_id and image_id in seen_image_ids and not allow_duplicate:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            if not image_id and stub and stub in seen_stubs and not allow_duplicate:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            if not image_id and stub and stub in json_stubs and not allow_duplicate:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            if self._is_recommendation_element(element, page_host, url_value) and source not in {'json_gallery', 'juxtapose'}:
                self.logger.debug(f"{skip_label}: recommendation block")
                return

            if host.endswith('youtube.com') or host.endswith('ytimg.com'):
                self.logger.debug(f"{skip_label}: external preview {host}")
                return

            if is_benchmark and source is None:
                self.logger.debug(f"{skip_label}: benchmark content skipped")
                return

            if self._is_avatar_block(element):
                self.logger.debug(f"{skip_label}: author avatar")
                return

            if is_benchmark and stub and stub in seen_strict_stubs:
                self.logger.debug(f"{skip_label}: {normalized}")
                return

            images.append(img)
            seen_urls.add(normalized)
            if image_id:
                seen_image_ids.add(image_id)
            if stub and (not image_id):
                seen_stubs.add(stub)
            if is_benchmark and stub:
                seen_strict_stubs.add(stub)
            if stub and img.get('source') == 'json_gallery':
                json_stubs.add(stub)
            self.logger.debug(f"{success_label}: {normalized}")

        hero_image = self.find_hero_image(soup, url)
        if hero_image:
            hero_image.setdefault('source', 'hero')
            hero_normalized = self._normalize_url(hero_image['url'])
            hero_id = hero_image.get('image_id') or self._extract_image_id(hero_image['url'])
            if hero_id:
                hero_ids.add(hero_id)
                hero_image['image_id'] = hero_id
            hero_stub = self._extract_image_stub(hero_image['url'])
            if hero_stub:
                hero_stubs.add(hero_stub)
            register(hero_image, "Обложка добавлена", "[ПРОПУСК] Дубль обложки")

        galleries = self._parse_json_galleries(soup)
        if galleries:
            for img in self._extract_gallery_images(galleries, url):
                register(img, "[JSON] Добавлено", "[ПРОПУСК] Дубль JSON")

        juxtapose_images = self._extract_juxtapose_images(soup)
        for img in juxtapose_images:
            register(img, "[Juxtapose] Добавлено", "[ПРОПУСК] Дубль Juxtapose")

        if page_host.endswith('zdnet.com'):
            for img in self._extract_zdnet_inline_images(soup):
                register(img, "[ZDNET] Добавлено", "[ПРОПУСК] Дубль ZDNET")

        soup_copy = BeautifulSoup(str(soup), 'html.parser')
        self._remove_excluded_elements(soup_copy, page_host)

        content_area = self._find_content_area(soup_copy)
        if not content_area:
            self.logger.warning("[ERROR] Не найден основной контент статьи")
            return images

        gallery_item_images = self._extract_gallery_item_images(content_area)
        for img in gallery_item_images:
            register(img, "[Gallery item] Добавлено", "[ПРОПУСК] Дубль gallery-item")

        content_images = self._extract_content_images(content_area, url)
        for img in content_images:
            register(img, "[CONTENT] Добавлено", "[ПРОПУСК] Дубль контента")

        self.logger.debug(f"=== ВСЕГО УНИКАЛЬНЫХ ИЗОБРАЖЕНИЙ: {len(images)} ===")
        return images

    def _remove_excluded_elements(self, soup: BeautifulSoup, page_host: Optional[str] = None) -> None:
        excluded_selectors = [
            'header nav', 'footer', 'aside',
            '.sidebar', '.advertisement', '.ad', '.promo',
            '.related-links', '.social-share', '.author-bio',
            '.newsletter', '.subscribe', '.comments',
            '.recommended-articles', '.recommendations-widget',
            '.popular-stories', '.latest-discussions',
            '.trending-articles', '.related-content',
            '.discussion-latest-content',
            '.back-to-top', '.scroll-to-top',
            '.cookie-banner', '.gdpr-banner'
        ]
        for selector in excluded_selectors:
            for element in soup.select(selector):
                element.decompose()

        if page_host and page_host.endswith('zdnet.com'):
            zdnet_selectors = ['[data-component="global-author"]', '.c-globalAuthor']
            for selector in zdnet_selectors:
                for element in soup.select(selector):
                    element.decompose()

    def _find_content_area(self, soup: BeautifulSoup) -> Optional[BeautifulSoup]:
        content_selectors = [
            'article', 'main', '.article', '.post', '.content',
            '.entry-content', '.post-content', '.article-body', '.single-content',
            '#review-body', 'div#review-body', '.review-page', '.review-section', '.review-article'
        ]
        for selector in content_selectors:
            content_area = soup.select_one(selector)
            if content_area:
                self.logger.debug(f"[OK] Найден контент в: {selector}")
                return content_area
        h1 = soup.find('h1')
        if h1 and h1.parent:
            self.logger.debug("[OK] Найден контент в: родитель H1")
            return h1.parent
        return None

    def _extract_content_images(self, content_area: BeautifulSoup, base_url: str) -> List[Dict]:
        images = []
        img_tags = content_area.find_all('img')
        self.logger.debug(f"Изображений в контентной области: {len(img_tags)}")

        for i, img in enumerate(img_tags, 1):
            img_url = self._get_image_url(img)
            if not img_url:
                continue
            alt_text = img.get('alt', '').strip()
            self.logger.debug(f"\n--- Контентное изображение {i} ---\nURL: {img_url}\nALT: {alt_text}")

            if self.is_tracking_pixel(img, img_url):
                continue
            if self.is_author_or_avatar(img, img_url, alt_text):
                self.logger.debug("    [ПРОПУСК] Аватар автора")
                continue
            if self.is_ui_element(img, img_url, alt_text):
                self.logger.debug("    [ПРОПУСК] UI элемент")
                continue

            full_url = urljoin(base_url, img_url)

            skip_reason = self._should_skip_by_url_pattern(full_url, base_url, element=img)
            if skip_reason:
                self.logger.debug(f"    [ПРОПУСК] {skip_reason}")
                continue

            if not self.validate_url(full_url):
                continue

            self.logger.debug("    [OK] ПРИНЯТО] Будет скачано")
            images.append({
                'url': full_url,
                'alt': alt_text,
                'element': img,
                'image_id': self._extract_image_id(full_url)
            })

        return images

    def _select_from_srcset(self, srcset: str) -> Optional[str]:
        if not srcset:
            return None
        candidates = []
        for entry in srcset.split(','):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split()
            url = parts[0]
            width = 0
            if len(parts) > 1:
                size = parts[-1]
                digits = ''.join(ch for ch in size if ch.isdigit())
                if digits:
                    try:
                        width = int(digits)
                    except ValueError:
                        width = 0
            candidates.append((width, url))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[-1][1]

    def _get_image_url(self, img_tag) -> Optional[str]:
        attribute_candidates = [
            img_tag.get('data-src'),
            img_tag.get('data-lazy-src'),
            img_tag.get('data-lazy'),
            img_tag.get('data-original'),
            img_tag.get('data-srcset'),
            img_tag.get('src'),
        ]

        first_candidate: Optional[str] = None
        for candidate in attribute_candidates:
            if candidate:
                candidate = candidate.strip()
                if candidate:
                    first_candidate = candidate
                    break

        anchor = img_tag.find_parent('a')
        anchor_candidate: Optional[str] = None
        onclick = ''
        if anchor:
            anchor_attrs = [
                anchor.get('data-src'),
                anchor.get('data-original'),
                anchor.get('data-full'),
                anchor.get('data-href'),
                anchor.get('href'),
            ]
            for candidate in anchor_attrs:
                if not candidate:
                    continue
                candidate = candidate.strip()
                if not candidate or candidate.startswith('#'):
                    continue
                if self._looks_like_image_url(candidate):
                    anchor_candidate = candidate
                    break

            onclick = anchor.get('onclick') or ''
            match = re.search(r'ShowImg2\(\s*[\'"]([^\'"]+)[\'"]\s*\)', onclick)
            if match:
                path = match.group(1).strip()
                if path:
                    anchor_candidate = path if path.startswith('http') else urljoin('https://fdn.gsmarena.com/imgroot/', path.lstrip('/'))

        if anchor_candidate:
            return anchor_candidate

        if first_candidate:
            return first_candidate

        if anchor:
            href = anchor.get('href')
            if href:
                href = href.strip()
                if href and href != '#' and self._looks_like_image_url(href):
                    return href

            if onclick:
                match = re.search(r'ShowImg2\(\s*[\'"]([^\'"]+)[\'"]\s*\)', onclick)
                if match:
                    path = match.group(1).strip()
                    if path:
                        return path if path.startswith('http') else urljoin('https://fdn.gsmarena.com/imgroot/', path.lstrip('/'))

        picture = img_tag.find_parent('picture')
        if picture:
            picture_candidates = [
                picture.get('data-srcset'),
                picture.get('data-lazy-srcset'),
                picture.get('srcset'),
                picture.get('data-original'),
                picture.get('data-src'),
            ]
            for candidate in picture_candidates:
                if candidate:
                    url = self._select_from_srcset(candidate) if (' ' in candidate or ',' in candidate) else candidate
                    if url:
                        return url

            for source in picture.find_all('source'):
                srcset_value = source.get('data-srcset') or source.get('srcset')
                if srcset_value:
                    url = self._select_from_srcset(srcset_value)
                    if url:
                        return url
                direct_value = source.get('data-src') or source.get('src')
                if direct_value:
                    direct_value = direct_value.strip()
                    if direct_value:
                        return direct_value

        return None

    def _parse_json_galleries(self, soup: BeautifulSoup) -> Dict:
        script_tag = soup.find('script', id='galleries-data')
        if not script_tag:
            return {}
        try:
            galleries_data = json.loads(script_tag.string)
            return galleries_data.get('galleries', {})
        except (json.JSONDecodeError, AttributeError) as e:
            self.logger.debug(f"Ошибка парсинга JSON галерей: {e}")
            return {}

    def _extract_gallery_images(self, galleries: Dict, base_url: str) -> List[Dict]:
        images = []
        cdn_url = "https://m-cdn.phonearena.com"

        for gallery_id, gallery_data in galleries.items():
            if 'data' not in gallery_data:
                continue

            self.logger.debug(f"Обработка галереи: {gallery_id}")

            for item in gallery_data['data']:
                if item.get('type') != 'image':
                    continue

                image_id = item.get('image_id')
                name = item.get('name', f'image_{image_id}')
                module = item.get('module', 'reviews')

                if not image_id:
                    continue

                img_url = f"{cdn_url}/images/{module}/{image_id}-image/{name}.webp"
                alt_text = item.get('pageTitle', item.get('name', 'Gallery image'))

                images.append({
                    'url': img_url,
                    'alt': alt_text,
                    'element': None,
                    'source': 'json_gallery',
                    'image_id': str(image_id)
                })

        if images:
            self.logger.debug(f"Извлечено {len(images)} изображений из JSON галерей")

        return images

    def _extract_juxtapose_images(self, soup: BeautifulSoup) -> List[Dict]:
        if not soup:
            return []

        images: List[Dict] = []
        seen_uids: Set[str] = set()
        attr_candidates = ['src', 'data-src', 'data-lazy-src', 'data-lazy', 'data-original']

        for iframe in soup.find_all('iframe'):
            slider_url = None
            for attr in attr_candidates:
                value = iframe.get(attr)
                if value and 'juxtapose' in value:
                    slider_url = value.strip()
                    break

            if not slider_url:
                continue

            parsed = urlparse(slider_url)
            query_pairs = dict(parse_qsl(parsed.query))
            uid = query_pairs.get('uid') or ''
            uid = unquote(uid).strip()
            if not uid:
                continue

            if uid.endswith('/'):
                uid = uid.rstrip('/')

            if uid.lower().startswith('http'):
                json_url: Optional[str] = uid
            else:
                json_url = f"https://s3.amazonaws.com/uploads.knightlab.com/juxtapose/{uid}.json"

            if json_url in seen_uids:
                continue
            seen_uids.add(json_url)

            try:
                response = self.session.get(json_url, timeout=15)
                response.raise_for_status()
                data = response.json()
            except curl_exceptions.RequestException as exc:
                self.logger.warning(f"[WARN] Juxtapose JSON не загружен: {json_url} ({exc})")
                continue
            except ValueError as exc:
                self.logger.warning(f"[WARN] Ошибка парсинга Juxtapose JSON: {json_url} ({exc})")
                continue

            slider_images = data.get('images', [])
            if not slider_images:
                continue

            self.logger.debug(f"Juxtapose {uid} содержит {len(slider_images)} изображений")
            for item in slider_images:
                src = item.get('src')
                if not src:
                    continue
                label = (item.get('label') or '').strip()
                credit = (item.get('credit') or '').strip()

                if label and credit and credit.lower() not in label.lower():
                    alt_text = f"{label} — {credit}"
                else:
                    alt_text = label or credit or 'Juxtapose image'

                images.append({
                    'url': src,
                    'alt': alt_text,
                    'element': iframe,
                    'source': 'juxtapose'
                })

        return images

    def _extract_gallery_item_images(self, content_area: BeautifulSoup) -> List[Dict]:
        images = []
        gallery_links = content_area.select('a.gallery-item')
        for link in gallery_links:
            href = link.get('href')
            if not href or not href.startswith('http'):
                continue
            img_tag = link.find('img')
            alt_text = img_tag.get('alt', 'Gallery image') if img_tag else 'Gallery image'
            images.append({
                'url': href,
                'alt': alt_text,
                'element': link,
                'source': 'gallery_item',
                'image_id': self._extract_image_id(href)
            })
        if images:
            self.logger.debug(f"Найдено {len(images)} изображений в gallery-item")
        return images

    def _extract_zdnet_inline_images(self, soup: BeautifulSoup) -> List[Dict]:
        script_content: Optional[str] = None
        for script in soup.find_all('script'):
            raw = script.string
            if not raw:
                continue
            if 'window.__NUXT__=' in raw:
                script_content = raw
                break

        if not script_content:
            return []

        try:
            decoded = script_content.encode('utf-8').decode('unicode_escape')
        except UnicodeDecodeError:
            return []

        uuid_to_path: Dict[str, str] = {}
        path_pattern = re.compile(r'id:\s*"([0-9a-f\-]{8,})"\s*,.*?path:\s*"([^"]+)"', re.IGNORECASE | re.DOTALL)
        for match in path_pattern.finditer(decoded):
            uuid = match.group(1)
            path = match.group(2)
            uuid_to_path[uuid] = path

        images: List[Dict] = []
        seen_urls: Set[str] = set()
        ordered_entries: List[Tuple[int, Dict]] = []
        shortcode_pattern = re.compile(r'<shortcode\s+shortcode="image"\s+([^>]+)>', re.IGNORECASE)
        attr_pattern = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')

        for match in shortcode_pattern.finditer(decoded):
            attr_text = match.group(1)
            attrs = {key: unescape(value) for key, value in attr_pattern.findall(attr_text)}
            uuid = attrs.get('uuid')
            if not uuid:
                continue
            url = uuid_to_path.get(uuid)
            if not url:
                filename = attrs.get('image-filename')
                if filename:
                    candidate = re.search(rf'https://www\.zdnet\.com/a/img/[^\s"]+/{re.escape(filename)}', decoded)
                    if candidate:
                        url = candidate.group(0)
            if not url:
                filename = attrs.get('image-filename')
                date_created = attrs.get('image-date-created', '').strip()
                if filename and date_created:
                    date_path = '/'.join(part.strip() for part in date_created.split('/') if part.strip())
                    if date_path:
                        url = f"https://www.zdnet.com/a/img/{date_path}/{uuid}/{filename}"
            if not url:
                continue
            ordered_entries.append((match.start(), {
                'url': url,
                'alt': attrs.get('image-alt-text', ''),
                'element': None,
                'source': 'zdnet_nuxt',
                'image_id': None,
            }))

        imagegroup_pattern = re.compile(r'imagegroup="([^"]+)"', re.IGNORECASE)
        for match in imagegroup_pattern.finditer(decoded):
            raw = unescape(match.group(1))
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            image_data = data.get('imageData') or {}
            url = image_data.get('path') or data.get('path')
            if not url:
                filename = image_data.get('filename') or data.get('imageFilename')
                date_created = data.get('imageDateCreated')
                uuid = data.get('uuid') or image_data.get('id')
                if filename and date_created and uuid:
                    date_path = '/'.join(part.strip() for part in date_created.replace('-', '/').split('/') if part.strip())
                    if date_path:
                        url = f"https://www.zdnet.com/a/img/{date_path}/{uuid}/{filename}"

            if not url:
                continue

            ordered_entries.append((match.start(), {
                'url': url,
                'alt': data.get('imageAltText') or data.get('alt') or image_data.get('alt', ''),
                'element': None,
                'source': 'zdnet_imagegroup',
                'image_id': image_data.get('id') or data.get('uuid'),
            }))

        ordered_entries.sort(key=lambda entry: entry[0])
        for _, img_data in ordered_entries:
            url = img_data['url']
            if not self.validate_url(url):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            images.append(img_data)

        return images

    def find_hero_image(self, soup: BeautifulSoup, url: str) -> Optional[Dict]:
        self.logger.debug("\n--- ПОИСК ОБЛОЖКИ СТАТЬИ ---")
        hero_selectors = [
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
            '.hero-image img', '.featured-image img', '.article-hero img',
            '.post-featured-image img', '.main-image img', '.header-image img',
            'header img', '.article-header img', '.post-header img',
            'h1 + * img', 'h1 ~ * img'
        ]
        for selector in hero_selectors:
            self.logger.debug(f"Проверяю: {selector}")
            if selector.startswith('meta'):
                hero_data = self._extract_meta_image(soup, url, selector)
                if hero_data:
                    return hero_data
            else:
                hero_data = self._extract_hero_image(soup, url, selector)
                if hero_data:
                    return hero_data
        self.logger.debug("[ERROR] Обложка не найдена")
        return None

    def _extract_meta_image(self, soup: BeautifulSoup, base_url: str, selector: str) -> Optional[Dict]:
        meta = soup.select_one(selector)
        if meta:
            img_url = meta.get('content')
            if img_url:
                full_url = urljoin(base_url, img_url)
                if self.validate_url(full_url):
                    self.logger.debug(f"[OK] НАЙДЕНА ОБЛОЖКА в мета-тегах: {full_url}")
                    return {
                        'url': full_url,
                        'alt': self.get_title_from_meta(soup) or 'Обложка статьи',
                        'element': meta,
                        'image_id': self._extract_image_id(full_url)
                    }
        return None

    def _extract_hero_image(self, soup: BeautifulSoup, base_url: str, selector: str) -> Optional[Dict]:
        img = soup.select_one(selector)
        if img:
            img_url = self._get_image_url(img)
            if img_url:
                alt_text = img.get('alt', '')
                if not self.is_author_or_avatar(img, img_url, alt_text):
                    full_url = urljoin(base_url, img_url)
                    if self.validate_url(full_url):
                        self.logger.debug(f"[OK] НАЙДЕНА ОБЛОЖКА: {full_url}")
                        return {
                            'url': full_url,
                            'alt': alt_text or 'Обложка статьи',
                            'element': img,
                            'image_id': self._extract_image_id(full_url)
                        }
        return None

    # ---------- ФИЛЬТРЫ ----------
    def is_tracking_pixel(self, img_tag, img_url: str) -> bool:
        tracking_domains = ['googletagmanager', 'facebook.com/tr', 'doubleclick.net/activity']
        img_url_lower = img_url.lower()
        for domain in tracking_domains:
            if domain in img_url_lower:
                self.logger.debug(f"    [ПРОПУСК] Пиксель отслеживания: {domain}")
                return True

        width = img_tag.get('width')
        height = img_tag.get('height')
        if width and height:
            try:
                w, h = int(width), int(height)
                if w <= 2 or h <= 2:
                    self.logger.debug(f"    [ПРОПУСК] Пиксель отслеживания: {w}x{h}")
                    return True
            except ValueError:
                pass
        return False

    def _is_recommendation_element(self, element, page_host: Optional[str] = None, img_url: Optional[str] = None) -> bool:
        if not element:
            return False

        block_keywords = [
            'popular-box', 'popular-data', 'popular-list', 'popular-box__article',
            'sidebar-popular', 'popular-box__article-list', 'pricing', 'widget', 'store',
            'promo', 'trending', 'deal', 'sponsored', 'affiliate', 'ads'
        ]

        ancestor = element
        depth = 0
        img_host = ''
        img_url_lower = (img_url or '').lower()
        if img_url:
            img_host = urlparse(img_url).netloc.lower()

        while ancestor is not None and depth < 5:
            classes = ancestor.get('class', [])
            if isinstance(classes, str):
                classes = [classes]
            class_str = ' '.join(classes).lower()

            if class_str:
                if 'hawk' in class_str and img_host and 'futurecdn.net' in img_host:
                    if 'logos' not in img_url_lower and 'merchant' not in img_url_lower:
                        return False

                if any(keyword in class_str for keyword in block_keywords):
                    return True

            ancestor = ancestor.parent
            depth += 1

        return False

    def _is_avatar_block(self, element) -> bool:
        if not element:
            return False
        ancestor = element
        depth = 0
        while ancestor is not None and depth < 6:
            classes = ' '.join(ancestor.get('class', [])).lower()
            if classes:
                has_avatar_marker = any(marker in classes for marker in ['avatar', 'profile', 'byline'])
                has_context_marker = any(marker in classes for marker in ['author', 'person', 'contributor', 'writer', 'staff'])
                if has_avatar_marker and has_context_marker:
                    return True
            ancestor = ancestor.parent
            depth += 1
        return False

    def is_author_or_avatar(self, img_tag, img_url: str, alt_text: str) -> bool:
        """Проверяет, является ли изображение аватаром автора (улучшенная логика)."""
        alt_lower = (alt_text or "").lower()
        url_lower = (img_url or "").lower()

        content_keywords = [
            'color', 'performance', 'device', 'view', 'comparison',
            'graph', 'benchmark', 'test', 'sample'
        ]
        if any(k in url_lower or k in alt_lower for k in content_keywords):
            return False

        author_phrases = [
            'author photo', 'author avatar', 'writer photo',
            'journalist photo', 'editor photo', 'by author',
            'profile picture', 'headshot', 'staff photo'
        ]
        matched_phrase = next((p for p in author_phrases if p in alt_lower), None)
        if matched_phrase:
            self.logger.debug(f"    [ПРОПУСК] Аватар автора по фразе: {matched_phrase}")
            return True

        strict_avatar_patterns = [
            '/avatar', 'avatar/', '-avatar',
            '/author', 'author/', '-author',
            '/headshot',
            '/staff',
            '/writer',
            '/users/',
            '/byline/',
            '/profile/',
            'user-profile',
            'author-profile',
            '-bio',
        ]
        for pattern in strict_avatar_patterns:
            if pattern in url_lower:
                self.logger.debug(f"    [ПРОПУСК] Аватар автора по URL: {pattern}")
                return True

        if img_tag:
            classes = ' '.join(img_tag.get('class', [])).lower()
            avatar_classes = ['author-avatar', 'author__avatar', 'staff-photo', 'writer-image', 'byline']
            for avatar_class in avatar_classes:
                if avatar_class in classes:
                    self.logger.debug(f"    [ПРОПУСК] Аватар по классу: {avatar_class}")
                    return True

            ancestor = img_tag.parent
            depth = 0
            while ancestor is not None and depth < 4:
                ancestor_classes = ' '.join(ancestor.get('class', [])).lower()
                if ancestor_classes and 'author' in ancestor_classes and ('avatar' in ancestor_classes or 'profile' in ancestor_classes):
                    self.logger.debug("    [SKIP] Avatar by ancestor class")
                    return True
                ancestor = ancestor.parent
                depth += 1

        return False

    def is_ui_element(self, img_tag, img_url: str, alt_text: str) -> bool:
        url_lower = img_url.lower()
        alt_lower = alt_text.lower()

        content_indicators = [
            'review', 'phone', 'device', 'smartphone', 'laptop',
            'side button', 'volume', 'power button', 'physical',
            'product', 'gadget', 'hardware'
        ]
        if any(indicator in alt_lower for indicator in content_indicators):
            return False

        ui_url_patterns = [
            '/button/', '/btn/', '/icon/', '/logo/', '/badge/',
            'button-', '-btn-', 'icon-', 'logo-',
            'google-news', 'follow', 'subscribe',
            'social-', 'share-', 'arrow-', 'chevron',
            'spinner', 'loader', 'placeholder',
            'bg-', 'watermark'
        ]
        for pattern in ui_url_patterns:
            if pattern in url_lower:
                self.logger.debug(f"    [ПРОПУСК] UI элемент в URL: {pattern}")
                return True

        ui_alt_patterns = [
            'follow us', 'subscribe', 'share button',
            'click here', 'download button', 'menu icon',
            'close button', 'next arrow', 'previous arrow'
        ]
        for pattern in ui_alt_patterns:
            if pattern in alt_lower:
                self.logger.debug(f"    [ПРОПУСК] UI элемент в ALT: {pattern}")
                return True

        if 'newsletter' in alt_lower:
            self.logger.debug("    [ПРОПУСК] Newsletter graphic")
            return True

        if img_tag:
            width = img_tag.get('width')
            height = img_tag.get('height')
            if width and height:
                try:
                    w, h = int(width), int(height)
                    if (w < 50 and h < 50) or (w < 20 or h < 20):
                        self.logger.debug(f"    [ПРОПУСК] Маленькая иконка: {w}x{h}")
                        return True
                except ValueError:
                    pass

        return False

    # ---------- ВАЛИДАЦИЯ/СКАЧИВАНИЕ ----------
    def is_valid_image(self, img_path: str) -> bool:
        """Валидация файла как изображения и проверка минимального размера."""
        try:
            with Image.open(img_path) as img:
                img.verify()

            with Image.open(img_path) as img:
                width, height = img.size

            if width < self.min_size or height < self.min_size:
                self.logger.debug(f"    [ФИЛЬТР] Слишком маленькое: {width}x{height}")
                return False

            self.logger.debug(f"    [OK] РАЗМЕР OK] {width}x{height}")
            return True

        except Exception as e:
            self.logger.debug(f"    [ОШИБКА] Не удалось открыть изображение: {e}")
            return False

    def _head_probe(self, img_url: str) -> Tuple[Optional[str], Optional[int]]:
        """HEAD-запрос для проверки content-type и content-length."""
        try:
            r = self.session.head(img_url, timeout=15, allow_redirects=True)
            if r.status_code >= 400:
                return None, None
            ctype = (r.headers.get('content-type') or '').lower()
            clen = r.headers.get('content-length')
            clen_int = int(clen) if clen and clen.isdigit() else None
            return ctype, clen_int
        except curl_exceptions.RequestException:
            return None, None

    def download_image(self, img_url: str, save_path: str) -> bool:
        success = False
        try:
            # Предварительный HEAD
            ctype, clen = self._head_probe(img_url)
            if ctype:
                if not ctype.startswith('image/'):
                    self.logger.warning(f"[WARN] Неверный content-type (HEAD): {ctype}")
                    return False
                if 'svg' in ctype:
                    self.logger.warning("[WARN] SVG изображения пропускаются из соображений безопасности (HEAD)")
                    return False
            if clen is not None:
                size_mb = clen / (1024 * 1024)
                if size_mb > self.MAX_FILE_SIZE_MB:
                    self.logger.warning(f"[WARN] Файл слишком большой (HEAD): {size_mb:.2f} MB")
                    return False

            # Основной GET
            response = self.session.get(img_url, timeout=30)
            response.raise_for_status()

            content_type = (response.headers.get('content-type') or '').lower()
            if content_type and not content_type.startswith('image/'):
                self.logger.warning(f"[WARN] Неверный content-type: {content_type}")
                return False
            if content_type and 'svg' in content_type:
                self.logger.warning("[WARN] SVG изображения пропускаются из соображений безопасности")
                return False

            content_length = response.headers.get('content-length')
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                if size_mb > self.MAX_FILE_SIZE_MB:
                    self.logger.warning(f"[WARN] Файл слишком большой: {size_mb:.2f} MB")
                    return False

            # Записываем контент
            content = response.content
            if len(content) > self.MAX_FILE_SIZE_MB * 1024 * 1024:
                self.logger.warning("[WARN] Превышен лимит размера при скачивании")
                return False

            hasher = hashlib.sha1(content) if self.hash_dedup else None

            with open(save_path, 'wb') as f:
                f.write(content)

            # Хеш-дедупликация
            if hasher:
                digest = hasher.hexdigest()
                if digest in self._seen_hashes:
                    self.logger.warning("[WARN] Дубликат по содержимому (sha1) — файл удалён")
                    try:
                        os.remove(save_path)
                    except Exception:
                        pass
                    return False
                self._seen_hashes.add(digest)

            # Валидация изображения
            if self.is_valid_image(save_path):
                success = True
                return True
            else:
                os.remove(save_path)
                return False

        except curl_exceptions.Timeout:
            self.logger.error(f"[ERROR] Таймаут при скачивании: {img_url}")
            return False
        except curl_exceptions.HTTPError as e:
            self.logger.error(f"[ERROR] HTTP ошибка: {img_url}")
            return False
        except curl_exceptions.RequestException as e:
            self.logger.error(f"[ERROR] Ошибка сети при скачивании {img_url}: {e}")
            return False
        except KeyboardInterrupt:
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass
            raise
        except IOError as e:
            self.logger.error(f"[ERROR] Ошибка записи файла {save_path}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"[ERROR] Неожиданная ошибка при скачивании {img_url}: {e}")
            return False
        finally:
            if not success and os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass

    # ---------- ОСНОВНОЙ ПРОЦЕСС ----------
    def process_article(self, url: str) -> List[str]:
        print(f"\n{'='*70}")
        print(f"Обрабатываем: {url}")
        print(f"{'='*70}")

        if not self.validate_url(url):
            self.logger.error("[ERROR] Невалидный или небезопасный URL")
            return []

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            page_urls = self._collect_article_pages(soup, url)

            if len(page_urls) > self.MAX_PAGES:
                self.logger.warning(f"[WARN] Найдено {len(page_urls)} страниц, обрабатываем первые {self.MAX_PAGES}")
                page_urls = page_urls[:self.MAX_PAGES]

            soups = [(soup, url)]
            base_clean = self._strip_query_fragment(url)
            for page_url in page_urls:
                if self._strip_query_fragment(page_url) == base_clean:
                    continue
                try:
                    page_response = self.session.get(page_url, timeout=30)
                    page_response.raise_for_status()
                    soups.append((BeautifulSoup(page_response.text, 'html.parser'), page_url))
                except curl_exceptions.RequestException as page_error:
                    self.logger.warning(f"[WARN] Unable to load additional page: {page_url} ({page_error})")

            article_title = self.get_article_title(soup)
            article_dir = self.create_numbered_article_dir(article_title)

            images = []
            seen_normalized = set()
            for current_soup, current_url in soups:
                page_images = self.find_content_images(current_soup, current_url)
                for image in page_images:
                    normalized = self._normalize_url(image['url'])
                    page_host = urlparse(current_url).netloc.lower()
                    allow_duplicate = self._allow_duplicate_image(page_host, image.get('element'), image.get('source'))
                    if normalized in seen_normalized and not allow_duplicate:
                        continue
                    images.append(image)
                    seen_normalized.add(normalized)

            if not images:
                print('[ERROR] No images found')
                return []

            print(f"\nFound {len(images)} unique images to download")

            downloaded = self._download_images(images, article_dir)

            print(f"\nУспешно скачано: {len(downloaded)}/{len(images)} изображений")
            return downloaded

        except curl_exceptions.Timeout:
            self.logger.error(f"[ERROR] Таймаут при загрузке страницы: {url}")
            return []
        except curl_exceptions.HTTPError as e:
            self.logger.error(f"[ERROR] HTTP ошибка: {url}")
            return []
        except curl_exceptions.RequestException as e:
            self.logger.error(f"[ERROR] Ошибка при обработке {url}: {e}")
            return []
        except Exception as e:
            self.logger.error(f"[ERROR] Неожиданная ошибка при обработке {url}: {e}")
            return []

    def _download_images(self, images: List[Dict], article_dir: str) -> List[str]:
        downloaded = []
        for i, img_data in enumerate(images, 1):
            img_url = img_data['url']
            alt_text = img_data['alt']
            filename = self._generate_filename(img_url, alt_text, i)
            save_path = os.path.join(article_dir, filename)

            print(f"\n  [{i}/{len(images)}] Скачиваем: {filename}")

            if self.download_image(img_url, save_path):
                downloaded.append(save_path)
                print(f"    [OK] Успешно")
            else:
                print(f"    [ERROR] Ошибка или не прошло фильтр")

            if i < len(images):
                time.sleep(self.pause_between_downloads)
        return downloaded

    def _generate_filename(self, img_url: str, alt_text: str, index: int) -> str:
        parsed_url = urlparse(img_url)
        original_name = os.path.basename(parsed_url.path)
        if not original_name or '.' not in original_name:
            extension = '.jpg'
            name_part = f"image_{index}"
        else:
            name_part, extension = os.path.splitext(original_name)
        if alt_text:
            filename = f"{index:02d}_{self.clean_filename(alt_text)}{extension}"
        else:
            filename = f"{index:02d}_{self.clean_filename(name_part)}{extension}"
        return filename


# ---------- CLI ----------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Скачивание контентных изображений из статьи (curl_cffi edition)."
    )
    parser.add_argument('url', nargs='?', help='Один URL для обработки (если не указан, используется --urls-file)')
    parser.add_argument('--urls-file', '--urls', dest='urls_file', default='urls.txt',
                        help='Файл со списком URL-ов (по одному в строке) [по умолчанию: urls.txt]')
    parser.add_argument('--download-dir', default='downloaded_images', help='Папка для сохранения')
    parser.add_argument('--min-size', type=int, default=20, help='Минимальный размер картинки в пикселях')
    parser.add_argument('--max-size-mb', type=int, default=50, help='Лимит размера файла (МБ)')
    parser.add_argument('--max-pages', type=int, default=20, help='Максимум страниц для многостраничных статей')
    parser.add_argument('--pause', type=float, default=0.5, help='Пауза между скачиваниями (сек)')
    parser.add_argument('--hash-dedup', action='store_true', help='Включить дедупликацию по хешу содержимого')
    parser.add_argument('--log-file', default=None, help='Писать логи также в файл')
    parser.add_argument('--debug', action='store_true', help='Подробные логи')
    return parser.parse_args()


def run_single(downloader: ArticleImageDownloader, url: str) -> None:
    downloader.process_article(url)


def run_batch(downloader: ArticleImageDownloader, urls_path: str) -> None:
    try:
        with open(urls_path, 'r', encoding='utf-8') as f:
            urls = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"[ERROR] Файл {urls_path} не найден")
        sys.exit(1)

    if not urls:
        print("[ERROR] Файл пуст или не содержит URL")
        sys.exit(1)

    print(f"\n[START] Начинаем обработку {len(urls)} URL(s)\n")

    total_downloaded = 0
    for i, url in enumerate(urls, 1):
        print(f"\n{'#'*70}")
        print(f"# Статья {i}/{len(urls)}")
        print(f"{'#'*70}")
        downloaded = downloader.process_article(url)
        total_downloaded += len(downloaded)

    print(f"\n{'='*70}")
    print(f"ГОТОВО! Всего скачано изображений: {total_downloaded}")
    print(f"{'='*70}\n")


def main():
    args = parse_args()
    downloader = ArticleImageDownloader(
        download_dir=args.download_dir,
        min_size=args.min_size,
        debug=args.debug,
        pause_between_downloads=args.pause,
        max_file_size_mb=args.max_size_mb,
        max_pages=args.max_pages,
        hash_dedup=args.hash_dedup,
        log_file=args.log_file,
    )

    if args.url:
        run_single(downloader, args.url)
    else:
        run_batch(downloader, args.urls_file)


if __name__ == "__main__":
    main()
