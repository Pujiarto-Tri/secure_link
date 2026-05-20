"""
Web Scraper - Crawler untuk mengumpulkan konten dari website
Dengan concurrent requests untuk performa optimal
"""
import re
import time
import logging
import urllib3
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from typing import Set, List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
import threading
from collections import deque

import requests
from bs4 import BeautifulSoup

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# C-optimized extensions tuple
SKIP_EXTENSIONS = (
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.zip', '.rar', '.tar', '.gz', '.7z',
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico', '.webp', '.bmp',
    '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.wav',
    '.css', '.js', '.json', '.xml', '.rss', '.atom',
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
)

CONTENT_RE = re.compile(r'content|main|body', re.I)


class WebScraper:
    """Kelas untuk melakukan web scraping pada website target dengan concurrent requests"""
    
    def __init__(
        self, 
        base_domain: str,
        delay: float = 0.1,
        timeout: int = 15,
        max_pages: int = 100,
        scan_subdomains: bool = True,
        max_workers: int = 5
    ):
        """
        Initialize web scraper
        
        Args:
            base_domain: Domain utama target (e.g., 'lombokbaratkab.go.id')
            delay: Delay antara batch request dalam detik
            timeout: Timeout untuk setiap request (reduced to 15s)
            max_pages: Maksimum halaman yang akan di-scrape
            scan_subdomains: Apakah akan memindai subdomain
            max_workers: Jumlah concurrent threads (5 = 5 halaman sekaligus)
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
        
        # Connection pooling for better performance
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=self.max_workers,
            pool_maxsize=self.max_workers * 2,
            max_retries=1
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
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
    
    def extract_links(self, soup: BeautifulSoup, current_url: str) -> List[str]:
        """Mengekstrak semua link dari halaman"""
        links = []
        
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href'].strip()
            
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
            except:
                continue
        
        return list(set(links))
    
    def extract_content(self, soup: BeautifulSoup) -> Dict[str, str]:
        """Mengekstrak konten dari halaman (decomposes soup elements in-place)"""
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
            soup.find('main') or 
            soup.find('article') or 
            soup.find('div', {'id': CONTENT_RE}) or
            soup.find('div', {'class': CONTENT_RE})
        )
        
        if main_content:
            content = main_content.get_text(separator=' ', strip=True)
        else:
            body = soup.find('body')
            content = body.get_text(separator=' ', strip=True) if body else ''
        
        content = re.sub(r'\s+', ' ', content).strip()
        
        return {
            'title': title,
            'meta_description': meta_description,
            'content': content[:50000]
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
            links = self.extract_links(soup, final_url)
            
            # Extract outbound (external) links
            outbound_links = []
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href'].strip()
                if href and not href.startswith(('javascript:', 'mailto:', 'tel:', '#', 'data:')):
                    try:
                        abs_url = urljoin(final_url, href)
                        parsed_href = urlparse(abs_url)
                        if parsed_href.netloc:
                            href_domain = parsed_href.netloc.lower().replace('www.', '')
                            base_clean = self.base_domain.replace('www.', '')
                            if href_domain != base_clean and not href_domain.endswith('.' + base_clean):
                                outbound_links.append(abs_url)
                    except:
                        pass

            content_data = self.extract_content(soup)
            
            parsed_url = urlparse(final_url)
            
            return {
                'url': final_url,
                'domain': parsed_url.netloc,
                'title': content_data['title'],
                'meta_description': content_data['meta_description'],
                'content': content_data['content'],
                'http_status': response.status_code,
                'links': links,
                'outbound_links': outbound_links,
                'success': True,
                'error': None
            }
            
        except requests.exceptions.Timeout:
            return {
                'url': url,
                'success': False,
                'error': 'Timeout (15s)',
                'http_status': None,
                'links': [],
                'outbound_links': []
            }
        except requests.exceptions.ConnectionError as e:
            return {
                'url': url,
                'success': False,
                'error': f'Connection Error',
                'http_status': None,
                'links': [],
                'outbound_links': []
            }
        except requests.exceptions.HTTPError as e:
            return {
                'url': url,
                'success': False,
                'error': f'HTTP {e.response.status_code if e.response else "Error"}',
                'http_status': e.response.status_code if e.response else None,
                'links': [],
                'outbound_links': []
            }
        except Exception as e:
            return {
                'url': url,
                'success': False,
                'error': str(e)[:100],
                'http_status': None,
                'links': [],
                'outbound_links': []
            }
    
    def crawl(self, start_url: str, callback=None) -> List[Dict]:
        """
        Melakukan crawling dengan concurrent requests dan persistent pipeline
        
        Args:
            start_url: URL awal untuk memulai crawling
            callback: Callback function (result, pages_scraped, max_pages)
        """
        results = []
        normalized_start = self.normalize_url(start_url)
        urls_to_visit = deque([normalized_start])
        enqueued_urls = {normalized_start}
        
        logger.info(f"Starting concurrent crawl: {normalized_start}")
        logger.info(f"Max pages: {self.max_pages} | Workers: {self.max_workers}")
        
        # Set up a persistent ThreadPoolExecutor for streaming crawl execution
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            
            while (urls_to_visit or futures) and self.pages_scraped < self.max_pages and not self.cancelled:
                # 1. Submit outstanding URLs as long as we have workers and capacity
                while urls_to_visit and len(futures) < self.max_workers and self.pages_scraped + len(futures) < self.max_pages:
                    url = urls_to_visit.popleft()
                    self.visited_urls.add(url)
                    future = executor.submit(self.scrape_page, url)
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
                            
                            # Add new links to queue immediately
                            if result.get('success') and result.get('links') and not self.cancelled:
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
