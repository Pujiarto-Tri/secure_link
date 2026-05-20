"""
Web Scraper - Crawler untuk mengumpulkan konten dari website
Dengan concurrent requests untuk performa optimal
"""
import hashlib
import re
import time
import logging
import urllib3
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser
from typing import Set, List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
import threading
from collections import deque

import requests
from selectolax.parser import HTMLParser

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

SKIP_EXTENSIONS = (
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.zip', '.rar', '.tar', '.gz', '.7z',
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico', '.webp', '.bmp',
    '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.wav',
    '.css', '.js', '.json', '.xml', '.rss', '.atom',
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
)

CONTENT_RE = re.compile(r'content|main|body', re.I)

SKIP_TAGS = frozenset({'script', 'style', 'noscript', 'iframe', 'svg'})

# Sitemap patterns to probe beyond /sitemap.xml and /sitemap_index.xml
SITEMAP_PATHS = [
    '/sitemap.xml', '/sitemap_index.xml', '/sitemaps.xml',
    '/wp-sitemap.xml', '/wp-sitemap-posts-post-1.xml', '/sitemap-posts.xml',
    '/sitemap.xsl', '/sitemap.gz',
]


class WebScraper:
    """Kelas untuk melakukan web scraping pada website target dengan concurrent requests"""
    
    def __init__(
        self, 
        base_domain: str,
        delay: float = 0.1,
        timeout: int = 15,
        max_pages: int = 100,
        scan_subdomains: bool = True,
        max_workers: int = 5,
        max_body_size: int = 2_000_000,
    ):
        self.base_domain = base_domain.lower()
        self.base_domain = self.base_domain.replace('https://', '').replace('http://', '')
        self.base_domain = self.base_domain.split('/')[0]
        self.base_domain = self.base_domain.strip('/')
        
        self.delay = delay
        self.timeout = timeout
        self.max_pages = max_pages
        self.scan_subdomains = scan_subdomains
        self.max_workers = max_workers
        self.max_body_size = max_body_size
        
        self.visited_urls: Set[str] = set()
        self.seen_hashes: Set[str] = set()
        self.pages_scraped = 0
        self.lock = threading.Lock()
        self.cancelled = False
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.verify = False
        
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=self.max_workers,
            pool_maxsize=self.max_workers * 2,
            max_retries=1
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        # Adaptive backoff state (per host)
        self._host_errors: Dict[str, int] = {}
        self._host_backoff: Dict[str, float] = {}
        self._host_lock = threading.Lock()
        
        logger.info(f"WebScraper initialized: {self.base_domain} | Workers: {max_workers} | Timeout: {timeout}s")
    
    def cancel(self):
        """Cancel the current scan"""
        self.cancelled = True
        logger.info("Scan cancelled by user")
    
    def is_valid_url(self, url: str) -> bool:
        """Memeriksa apakah URL valid untuk di-scrape"""
        try:
            parsed = urlparse(url)
            
            if not parsed.scheme or not parsed.netloc:
                return False
            
            if parsed.scheme not in ['http', 'https']:
                return False
            
            domain = parsed.netloc.lower()
            domain_clean = domain.replace('www.', '')
            base_clean = self.base_domain.replace('www.', '')
            
            if self.scan_subdomains:
                if not (domain_clean == base_clean or domain_clean.endswith('.' + base_clean)):
                    return False
            else:
                if domain_clean != base_clean:
                    return False
            
            path_lower = parsed.path.lower()
            if path_lower.endswith(SKIP_EXTENSIONS):
                return False
            
            return True
            
        except Exception as e:
            return False
    
    def normalize_url(self, url: str) -> str:
        """Normalisasi URL untuk menghindari duplikasi"""
        try:
            url = url.split('#')[0]
            parsed = urlparse(url)
            
            # Lowercase scheme and netloc
            scheme = parsed.scheme.lower()
            netloc = parsed.netloc.lower()
            
            # Collapse trailing slashes in path
            path = parsed.path
            if path != '/':
                path = path.rstrip('/')
            if not path:
                path = '/'
                
            # Parse query params, drop tracking parameters and session IDs, and sort them
            query_params = parse_qsl(parsed.query)
            ignored_params = {
                'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
                'gclid', 'fbclid', 'phpsessid', 'sid', 'jsessionid'
            }
            filtered_params = [
                (k, v) for k, v in query_params 
                if k.lower() not in ignored_params
            ]
            filtered_params.sort()
            
            query = urlencode(filtered_params) if filtered_params else ''
            
            # Reconstruct URL without fragment
            return urlunparse((scheme, netloc, path, parsed.params, query, ''))
        except Exception:
            return url
    
    def extract_links(self, parser: HTMLParser, current_url: str) -> List[str]:
        """Mengekstrak semua link dari halaman (selectolax)"""
        links = []
        for node in parser.root.css('a[href]'):
            href = (node.attributes.get('href', '') or '').strip()
            if not href:
                continue
            if href.startswith(('javascript:', 'mailto:', 'tel:', '#', 'data:')):
                continue
            try:
                absolute_url = urljoin(current_url, href)
                normalized_url = self.normalize_url(absolute_url)
                if self.is_valid_url(normalized_url):
                    with self.lock:
                        if normalized_url not in self.visited_urls:
                            links.append(normalized_url)
            except Exception:
                continue
        return list(set(links))
    
    def _collect_text(self, node, skip_tags: frozenset) -> str:
        """Recursively collect text from a selectolax node, skipping specified tags."""
        if node.tag in skip_tags:
            return ''
        parts = []
        text = node.text(strip=True)
        if text:
            parts.append(text)
        for child in node.iter(include_text=False):
            if child.tag in skip_tags:
                continue
            child_text = self._collect_text(child, skip_tags)
            if child_text:
                parts.append(child_text)
        return ' '.join(parts)

    def extract_content(self, parser: HTMLParser) -> Dict[str, str]:
        """Mengekstrak konten dari halaman (selectolax)"""
        title = ''
        title_node = parser.root.css_first('title')
        if title_node:
            title = title_node.text(strip=True)
        
        meta_description = ''
        meta_node = parser.root.css_first('meta[name="description"]')
        if meta_node:
            meta_description = meta_node.attributes.get('content', '') or ''
        
        main_node = (
            parser.root.css_first('main') or
            parser.root.css_first('article') or
            parser.root.css_first('div[role="main"]')
        )
        if not main_node:
            for div in parser.root.css('div'):
                div_id = (div.attributes.get('id', '') or '').lower()
                div_cls = (div.attributes.get('class', '') or '').lower()
                if CONTENT_RE.search(div_id) or CONTENT_RE.search(div_cls):
                    main_node = div
                    break
        
        if main_node:
            content = self._collect_text(main_node, SKIP_TAGS)
        else:
            body = parser.root.css_first('body')
            content = self._collect_text(body, SKIP_TAGS) if body else ''
        
        content = re.sub(r'\s+', ' ', content).strip()
        
        return {
            'title': title,
            'meta_description': meta_description,
            'content': content[:50000]
        }
    
    def _detect_cloaking(self, parser: HTMLParser) -> Tuple[bool, List[str]]:
        """Detect cloaking/hidden content signals on a page."""
        cloaked_snippets = []
        
        inline_hidden = re.compile(
            r'display\s*:\s*none|visibility\s*:\s*hidden|'
            r'font-size\s*:\s*0|opacity\s*:\s*0|'
            r'position\s*:\s*absolute\s*;\s*left\s*:\s*-9999',
            re.I
        )
        zero_width_chars = re.compile(r'[\u200b\u200c\u200d\ufeff]')
        
        for node in parser.root.traverse(include_text=False):
            style = (node.attributes.get('style', '') or '')
            if inline_hidden.search(style):
                text = node.text(strip=True)
                if text:
                    cloaked_snippets.append(text[:200])
        
        root_text = parser.root.text(strip=False) if parser.root else ''
        zw_matches = zero_width_chars.findall(root_text)
        if len(zw_matches) >= 3:
            cloaked_snippets.append(f'{len(zw_matches)} zero-width characters detected')
        
        has_cloaking = len(cloaked_snippets) > 0
        return has_cloaking, cloaked_snippets

    def _host_wait(self, host: str) -> float:
        """Get the backoff delay for a host (adaptive rate limiting)."""
        with self._host_lock:
            return self._host_backoff.get(host, 0.0)

    def _record_host_error(self, host: str):
        """Record an error for a host and increase backoff."""
        with self._host_lock:
            self._host_errors[host] = self._host_errors.get(host, 0) + 1
            errors = self._host_errors[host]
            if errors >= 3:
                self._host_backoff[host] = min(errors * 2.0, 30.0)

    def _record_host_success(self, host: str):
        """Reset error count and backoff for a host on success."""
        with self._host_lock:
            self._host_errors.pop(host, None)
            self._host_backoff.pop(host, None)

    def _probe_head(self, url: str) -> Optional[Dict]:
        """Issue a HEAD request to check content type and size before downloading."""
        try:
            resp = self.session.head(url, timeout=10, allow_redirects=True)
            content_type = resp.headers.get('Content-Type', '')
            if 'text/html' not in content_type.lower():
                return {
                    'reason': f'Non-HTML content type: {content_type[:80]}',
                    'should_skip': True,
                }
            content_length = resp.headers.get('Content-Length')
            if content_length:
                try:
                    cl = int(content_length)
                    if cl > self.max_body_size:
                        return {
                            'reason': f'Content too large: {cl} bytes',
                            'should_skip': True,
                        }
                except ValueError:
                    pass
            return {'should_skip': False}
        except Exception:
            return None

    def scrape_page(self, url: str, stored_etag: str = '', stored_last_modified: str = '') -> Optional[Dict]:
        """Melakukan scraping pada satu halaman (selectolax, HEAD probe, hash dedup, cloaking)"""
        if self.cancelled:
            return None
        
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        
        backoff = self._host_wait(host)
        if backoff > 0:
            time.sleep(backoff)
        
        # HEAD probe for content-type + size before full download
        probe = self._probe_head(url)
        if probe and probe.get('should_skip'):
            return {
                'url': url,
                'success': False,
                'error': probe.get('reason', 'Skipped by HEAD probe'),
                'http_status': None,
                'links': [],
                'outbound_links': [],
            }
            
        try:
            request_headers = {}
            if stored_etag:
                request_headers['If-None-Match'] = stored_etag
            if stored_last_modified:
                request_headers['If-Modified-Since'] = stored_last_modified
            
            response = self.session.get(
                url, 
                timeout=self.timeout, 
                allow_redirects=True,
                verify=False,
                stream=True,
                headers=request_headers if request_headers else None
            )
            
            if response.status_code in (429, 503):
                self._record_host_error(host)
                retry_after = response.headers.get('Retry-After')
                if retry_after:
                    try:
                        retry_seconds = int(retry_after)
                        with self._host_lock:
                            self._host_backoff[host] = min(retry_seconds, 60.0)
                    except ValueError:
                        pass
                return {
                    'url': url,
                    'success': False,
                    'error': f'Rate limited (HTTP {response.status_code})',
                    'http_status': response.status_code,
                    'links': [],
                    'outbound_links': [],
                }
            
            if response.status_code == 304:
                self._record_host_success(host)
                return {
                    'url': url,
                    'success': True,
                    'http_status': 304,
                    'links': [],
                    'outbound_links': [],
                    'unchanged': True,
                }
            
            response.raise_for_status()
            
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' not in content_type.lower():
                return {
                    'url': url,
                    'success': False,
                    'error': f'Non-HTML content type: {content_type[:80]}',
                    'http_status': response.status_code,
                    'links': [],
                    'outbound_links': [],
                }
            
            # Read body with size cap
            chunks = []
            total_bytes = 0
            for chunk in response.iter_content(chunk_size=8192, decode_unicode=False):
                if chunk:
                    total_bytes += len(chunk)
                    if total_bytes > self.max_body_size:
                        return {
                            'url': url,
                            'success': False,
                            'error': f'Content exceeds size limit ({self.max_body_size} bytes)',
                            'http_status': response.status_code,
                            'links': [],
                            'outbound_links': [],
                        }
                    chunks.append(chunk)
            
            raw_text = b''.join(chunks).decode(response.encoding or 'utf-8', errors='replace')
            
            final_url = response.url
            
            # Content hash dedup
            content_hash = hashlib.sha1(raw_text.encode('utf-8', errors='replace')).hexdigest()
            with self.lock:
                if content_hash in self.seen_hashes:
                    return {
                        'url': final_url,
                        'success': True,
                        'http_status': response.status_code,
                        'links': [],
                        'outbound_links': [],
                        'content_hash': content_hash,
                        'duplicate_hash': True,
                    }
                self.seen_hashes.add(content_hash)
            
            parser = HTMLParser(raw_text)
            links = self.extract_links(parser, final_url)
            
            # Outbound links
            outbound_links = []
            base_clean = self.base_domain.replace('www.', '')
            for node in parser.root.css('a[href]'):
                href = (node.attributes.get('href', '') or '').strip()
                if not href or href.startswith(('javascript:', 'mailto:', 'tel:', '#', 'data:')):
                    continue
                try:
                    abs_url = urljoin(final_url, href)
                    parsed_href = urlparse(abs_url)
                    if parsed_href.netloc:
                        href_domain = parsed_href.netloc.lower().replace('www.', '')
                        if href_domain != base_clean and not href_domain.endswith('.' + base_clean):
                            outbound_links.append(abs_url)
                except Exception:
                    pass

            content_data = self.extract_content(parser)
            has_cloaking, cloaked_snippets = self._detect_cloaking(parser)
            
            self._record_host_success(host)
            
            parsed_url = urlparse(final_url)
            
            return {
                'url': final_url,
                'domain': parsed_url.netloc,
                'title': content_data['title'],
                'meta_description': content_data['meta_description'],
                'content': content_data['content'],
                'raw_text': raw_text,
                'http_status': response.status_code,
                'links': links,
                'outbound_links': outbound_links,
                'content_hash': content_hash,
                'etag': response.headers.get('ETag', ''),
                'last_modified': response.headers.get('Last-Modified', ''),
                'has_cloaking': has_cloaking,
                'cloaked_snippets': cloaked_snippets,
                'success': True,
                'error': None
            }
            
        except requests.exceptions.Timeout:
            self._record_host_error(host)
            return {
                'url': url,
                'success': False,
                'error': 'Timeout',
                'http_status': None,
                'links': [],
                'outbound_links': [],
            }
        except requests.exceptions.ConnectionError as e:
            self._record_host_error(host)
            return {
                'url': url,
                'success': False,
                'error': 'Connection Error',
                'http_status': None,
                'links': [],
                'outbound_links': [],
            }
        except requests.exceptions.HTTPError as e:
            self._record_host_error(host)
            return {
                'url': url,
                'success': False,
                'error': f'HTTP {e.response.status_code if e.response else "Error"}',
                'http_status': e.response.status_code if e.response else None,
                'links': [],
                'outbound_links': [],
            }
        except Exception as e:
            self._record_host_error(host)
            return {
                'url': url,
                'success': False,
                'error': str(e)[:100],
                'http_status': None,
                'links': [],
                'outbound_links': [],
            }
    
    def fetch_sitemap_urls(self) -> List[str]:
        """Fetch URLs from sitemaps before starting BFS crawl."""
        sitemap_urls = set()
        base_url = f'https://{self.base_domain}'
        
        # Try robots.txt for Sitemap: directives first
        try:
            resp = self.session.get(f'{base_url}/robots.txt', timeout=10)
            if resp.status_code == 200:
                for line in resp.text.splitlines():
                    line_lower = line.strip().lower()
                    if line_lower.startswith('sitemap:'):
                        sitemap_url = line.strip().split(':', 1)[1].strip()
                        sitemap_urls.add(sitemap_url)
        except Exception:
            pass
        
        # Probe known sitemap paths
        for path in SITEMAP_PATHS:
            if self.cancelled:
                break
            try:
                resp = self.session.get(f'{base_url}{path}', timeout=10)
                if resp.status_code == 200:
                    sitemap_urls.add(f'{base_url}{path}')
            except Exception:
                continue
        
        if not sitemap_urls:
            return []
        
        # Parse each sitemap for URLs
        from xml.etree import ElementTree
        discovered = []
        ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        
        for sitemap_url in sitemap_urls:
            if self.cancelled:
                break
            try:
                resp = self.session.get(sitemap_url, timeout=15)
                if resp.status_code != 200:
                    continue
                
                root = ElementTree.fromstring(resp.content)
                # Check if this is a sitemap index (points to other sitemaps)
                sub_sitemaps = root.findall('sm:sitemap/sm:loc', ns)
                if sub_sitemaps:
                    for sub in sub_sitemaps:
                        if self.cancelled:
                            break
                        try:
                            sub_url = sub.text.strip()
                            sub_resp = self.session.get(sub_url, timeout=15)
                            if sub_resp.status_code == 200:
                                sub_root = ElementTree.fromstring(sub_resp.content)
                                for url_el in sub_root.findall('sm:url/sm:loc', ns):
                                    normalized = self.normalize_url(url_el.text.strip())
                                    if self.is_valid_url(normalized):
                                        discovered.append(normalized)
                        except Exception:
                            continue
                else:
                    for url_el in root.findall('sm:url/sm:loc', ns):
                        normalized = self.normalize_url(url_el.text.strip())
                        if self.is_valid_url(normalized):
                            discovered.append(normalized)
            except Exception:
                continue
        
        logger.info(f"Sitemap discovered {len(discovered)} URLs from {len(sitemap_urls)} sitemap(s)")
        return discovered

    def crawl(self, start_url: str, callback=None, stored_metadata: Dict[str, Tuple[str, str]] = None) -> List[Dict]:
        """
        Melakukan crawling dengan concurrent requests dan persistent pipeline
        
        Args:
            start_url: URL awal untuk memulai crawling
            callback: Callback function (result, pages_scraped, max_pages)
            stored_metadata: Dict of normalized_url -> (etag, last_modified) from prior scans
        """
        results = []
        normalized_start = self.normalize_url(start_url)
        urls_to_visit = deque([normalized_start])
        enqueued_urls = {normalized_start}
        
        # Sitemap-first seeding: add sitemap URLs before starting BFS
        sitemap_urls = self.fetch_sitemap_urls()
        for sitemap_url in sitemap_urls:
            if sitemap_url not in enqueued_urls:
                enqueued_urls.add(sitemap_url)
                urls_to_visit.append(sitemap_url)
        
        logger.info(f"Starting concurrent crawl: {normalized_start}")
        logger.info(f"Max pages: {self.max_pages} | Workers: {self.max_workers} | Queue size: {len(urls_to_visit)}")
        
        if stored_metadata is None:
            stored_metadata = {}
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            
            while (urls_to_visit or futures) and self.pages_scraped < self.max_pages and not self.cancelled:
                while urls_to_visit and len(futures) < self.max_workers and self.pages_scraped + len(futures) < self.max_pages:
                    url = urls_to_visit.popleft()
                    self.visited_urls.add(url)
                    stored_etag, stored_lm = stored_metadata.get(url, ('', ''))
                    future = executor.submit(self.scrape_page, url, stored_etag, stored_lm)
                    futures[future] = url
                
                if not futures:
                    break
                
                # 2. Wait for at least one worker to finish
                done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)
                
                for future in done:
                    url = futures.pop(future)
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                            with self.lock:
                                self.pages_scraped += 1
                            
                            logger.info(f"[{self.pages_scraped}/{self.max_pages}] {url[:60]}...")
                            
                            if callback:
                                callback(result, self.pages_scraped, self.max_pages)
                            
                            # Add new links to queue immediately (skip duplicates by hash)
                            if result.get('success') and result.get('links') and not self.cancelled and not result.get('duplicate_hash'):
                                for link in result['links']:
                                    if link not in self.visited_urls and link not in enqueued_urls:
                                        enqueued_urls.add(link)
                                        urls_to_visit.append(link)
                                        
                    except Exception as e:
                        logger.error(f"Error processing {url}: {e}")
                
                # Delay between fetches if needed
                if self.delay > 0 and not self.cancelled and urls_to_visit:
                    time.sleep(self.delay)
        
        status = "cancelled" if self.cancelled else "complete"
        logger.info(f"Crawl {status}. Total pages: {len(results)}")
        return results
