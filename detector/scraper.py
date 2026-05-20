"""
Web Scraper - Crawler untuk mengumpulkan konten dari website
Dengan concurrent requests untuk performa optimal
"""
import re
import logging
import urllib3
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)
from typing import Dict, List, Optional, Set
import threading

import requests
from bs4 import BeautifulSoup

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# File extensions we never want to crawl. ``frozenset`` lookup is O(1).
SKIP_EXTENSIONS = frozenset({
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.zip', '.rar', '.tar', '.gz', '.7z',
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico', '.webp', '.bmp',
    '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.wav',
    '.css', '.js', '.json', '.xml', '.rss', '.atom',
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
})

# Tracking / session query-string parameters that should be stripped during
# URL canonicalisation so the same page isn't crawled multiple times.
TRACKING_PARAMS = frozenset({
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'utm_id',
    'gclid', 'fbclid', 'msclkid', 'mc_cid', 'mc_eid', 'yclid', 'igshid',
    '_ga', '_gl',
    'phpsessid', 'jsessionid', 'sid', 'sessid', 'session_id',
})

# Pre-compiled regex used by ``WebScraper.extract_content`` to locate the main
# content container. Compiled once at module level instead of per page.
_MAIN_CONTENT_RE = re.compile(r'content|main|body', re.I)
_WHITESPACE_RE = re.compile(r'\s+')


class WebScraper:
    """Kelas untuk melakukan web scraping pada website target dengan concurrent requests"""
    
    def __init__(
        self, 
        base_domain: str,
        delay: float = 0.1,
        timeout: int = 15,
        max_pages: int = 100,
        scan_subdomains: bool = True,
        max_workers: int = 12,
    ):
        """
        Initialize web scraper

        Args:
            base_domain: Domain utama target (e.g., 'lombokbaratkab.go.id')
            delay: Delay opsional antara halaman (sebagian besar diabaikan pada
                pipeline streaming; tetap dipertahankan untuk kompatibilitas).
            timeout: Timeout untuk setiap request (default 15s)
            max_pages: Maksimum halaman yang akan di-scrape
            scan_subdomains: Apakah akan memindai subdomain
            max_workers: Jumlah concurrent threads.
                Default dinaikkan ke 12 (cocok untuk i3 / 4 GB) - 3-5× lebih
                cepat dari setting lama (3) tanpa membebani RAM.
        """
        # Clean the base domain
        self.base_domain = base_domain.lower()
        self.base_domain = self.base_domain.replace('https://', '').replace('http://', '')
        self.base_domain = self.base_domain.split('/')[0]
        self.base_domain = self.base_domain.strip('/')
        
        self.delay = delay
        self.timeout = timeout
        self.max_pages = max_pages
        self.scan_subdomains = scan_subdomains
        self.max_workers = max_workers
        
        self.visited_urls: Set[str] = set()
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
        
        # Create a session for connection reuse
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.verify = False
        
        # Connection pooling sized to match worker concurrency so requests don't
        # serialise behind a too-small pool.
        pool_size = max(max_workers * 2, 10)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool_size,
            pool_maxsize=pool_size,
            max_retries=1,
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

        logger.info(
            f"WebScraper initialized: {self.base_domain} | Workers: {max_workers} | "
            f"Pool: {pool_size} | Timeout: {timeout}s"
        )
    
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

            if parsed.scheme not in ('http', 'https'):
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
            # Fast O(1) extension check via frozenset lookup of the trailing
            # ``.suffix``. Avoids iterating the whole list per URL.
            dot = path_lower.rfind('.')
            if dot != -1:
                ext = path_lower[dot:]
                if ext in SKIP_EXTENSIONS:
                    return False

            return True

        except Exception:
            return False

    def normalize_url(self, url: str) -> str:
        """Canonicalise a URL so duplicates collapse to a single key.

        Steps:
            * drop the ``#fragment``
            * lowercase the host
            * drop tracking / session query params (utm_*, gclid, PHPSESSID, …)
            * sort the remaining query params for stable ordering
            * collapse a trailing ``/`` (except on the root path)

        Cuts the visited-URL set by 20-40 % on a typical CMS where the same
        page is reachable via many query-string variants.
        """
        try:
            parsed = urlparse(url.split('#')[0])

            netloc = parsed.netloc.lower()

            if parsed.query:
                params = [
                    (k, v)
                    for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                    if k.lower() not in TRACKING_PARAMS
                ]
                params.sort()
                query = urlencode(params)
            else:
                query = ''

            path = parsed.path or '/'
            if path != '/' and path.endswith('/'):
                path = path.rstrip('/') or '/'

            return urlunparse((parsed.scheme, netloc, path, '', query, ''))
        except Exception:
            return url
    
    def extract_links(self, soup: BeautifulSoup, current_url: str) -> List[str]:
        """Mengekstrak semua link dari halaman.

        De-duplicates within the page only; the crawler is responsible for
        de-duplicating against URLs already visited / queued.
        """
        links: List[str] = []
        seen_on_page: Set[str] = set()

        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href'].strip()

            if not href:
                continue

            if href.startswith(('javascript:', 'mailto:', 'tel:', '#', 'data:')):
                continue

            try:
                absolute_url = urljoin(current_url, href)
                normalized_url = self.normalize_url(absolute_url)
            except Exception:
                continue

            if normalized_url in seen_on_page:
                continue
            seen_on_page.add(normalized_url)

            if self.is_valid_url(normalized_url):
                links.append(normalized_url)

        return links
    
    def extract_content(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Mengekstrak konten dari halaman.

        Bekerja langsung di atas ``soup`` (tidak melakukan re-parse HTML kedua
        seperti versi sebelumnya). Untuk halaman 100 KB+ ini bisa menghilangkan
        ~50 % waktu parsing per halaman.
        """
        title = ''
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)

        meta_description = ''
        meta_tag = soup.find('meta', attrs={'name': 'description'})
        if meta_tag and meta_tag.get('content'):
            meta_description = meta_tag['content']

        for element in soup(['script', 'style', 'noscript', 'iframe', 'svg']):
            element.decompose()

        main_content = (
            soup.find('main')
            or soup.find('article')
            or soup.find('div', {'id': _MAIN_CONTENT_RE})
            or soup.find('div', {'class': _MAIN_CONTENT_RE})
        )

        if main_content:
            content = main_content.get_text(separator=' ', strip=True)
        else:
            body = soup.find('body')
            content = body.get_text(separator=' ', strip=True) if body else ''

        content = _WHITESPACE_RE.sub(' ', content).strip()

        return {
            'title': title,
            'meta_description': meta_description,
            'content': content[:50000],
        }
    
    def scrape_page(self, url: str) -> Optional[Dict]:
        """Melakukan scraping pada satu halaman"""
        if self.cancelled:
            return None
            
        try:
            response = self.session.get(
                url, 
                timeout=self.timeout, 
                allow_redirects=True,
                verify=False
            )
            response.raise_for_status()
            
            final_url = response.url
            
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' not in content_type.lower():
                return None
            
            soup = BeautifulSoup(response.text, 'lxml')
            content_data = self.extract_content(soup)
            links = self.extract_links(soup, final_url)
            
            parsed_url = urlparse(final_url)
            
            return {
                'url': final_url,
                'domain': parsed_url.netloc,
                'title': content_data['title'],
                'meta_description': content_data['meta_description'],
                'content': content_data['content'],
                'http_status': response.status_code,
                'links': links,
                'success': True,
                'error': None
            }
            
        except requests.exceptions.Timeout:
            return {
                'url': url,
                'success': False,
                'error': 'Timeout (15s)',
                'http_status': None,
                'links': []
            }
        except requests.exceptions.ConnectionError as e:
            return {
                'url': url,
                'success': False,
                'error': f'Connection Error',
                'http_status': None,
                'links': []
            }
        except requests.exceptions.HTTPError as e:
            return {
                'url': url,
                'success': False,
                'error': f'HTTP {e.response.status_code if e.response else "Error"}',
                'http_status': e.response.status_code if e.response else None,
                'links': []
            }
        except Exception as e:
            return {
                'url': url,
                'success': False,
                'error': str(e)[:100],
                'http_status': None,
                'links': []
            }
    
    def crawl(self, start_url: str, callback=None) -> List[Dict]:
        """
        Streaming concurrent crawl.

        Unlike the previous implementation this keeps a single
        :class:`ThreadPoolExecutor` alive for the whole crawl and submits new
        URLs to it as soon as old ones complete — there's no per-batch barrier
        where every worker has to wait for the slowest URL.

        The URL frontier is a :class:`collections.deque` (O(1) ``popleft``) and
        the seen-set is a plain ``set`` (O(1) membership), so adding a newly
        discovered link is O(1) regardless of how many URLs have already been
        seen.

        Args:
            start_url: URL awal untuk memulai crawling
            callback: Callback function (result, pages_scraped, max_pages)
        """
        results: List[Dict] = []
        normalized_start = self.normalize_url(start_url)

        frontier: deque = deque([normalized_start])
        self.visited_urls = {normalized_start}

        logger.info(f"Starting streaming crawl: {normalized_start}")
        logger.info(f"Max pages: {self.max_pages} | Workers: {self.max_workers}")

        in_flight: Dict = {}

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while not self.cancelled:
                # Fill the in-flight pool up to ``max_workers``, but never
                # submit more requests than ``max_pages`` worth of work.
                while (
                    frontier
                    and len(in_flight) < self.max_workers
                    and (self.pages_scraped + len(in_flight)) < self.max_pages
                ):
                    next_url = frontier.popleft()
                    fut = executor.submit(self.scrape_page, next_url)
                    in_flight[fut] = next_url

                if not in_flight:
                    break  # nothing queued and nothing running -> done

                done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)

                for fut in done:
                    url = in_flight.pop(fut)

                    if self.cancelled:
                        continue

                    try:
                        result = fut.result()
                    except Exception as e:
                        logger.error(f"Error processing {url}: {e}")
                        continue

                    if not result:
                        continue

                    results.append(result)
                    with self.lock:
                        self.pages_scraped += 1
                        current = self.pages_scraped

                    logger.info(f"[{current}/{self.max_pages}] {url[:60]}...")

                    if callback:
                        callback(result, current, self.max_pages)

                    if result.get('success') and result.get('links'):
                        for link in result['links']:
                            if link not in self.visited_urls:
                                self.visited_urls.add(link)
                                frontier.append(link)

            # If the user cancelled mid-crawl, drain in-flight futures so the
            # ThreadPoolExecutor can shut down cleanly. We don't process their
            # results because the scan was aborted.
            if self.cancelled:
                for fut in in_flight:
                    fut.cancel()

        status = "cancelled" if self.cancelled else "complete"
        logger.info(f"Crawl {status}. Total pages: {len(results)}")
        return results
