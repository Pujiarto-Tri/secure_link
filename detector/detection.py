"""
Content Detector - Engine untuk mendeteksi konten negatif
"""
import re
import ahocorasick
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlparse


class ContentDetector:
    """Kelas untuk mendeteksi konten negatif berdasarkan kata kunci"""
    
    # Kata kunci default
    DEFAULT_KEYWORDS = {
        'judol': [
            'slot', 'togel', 'poker', 'casino', 'jackpot', 'gacor', 'maxwin',
            'rtp live', 'scatter', 'pragmatic', 'pg soft', 'olympus', 'mahjong ways',
            'sweet bonanza', 'gates of olympus', 'starlight princess', 'wild west gold',
            'deposit pulsa', 'slot online', 'judi online', 'taruhan', 'betting',
            'bandar togel', 'bandar bola', 'sportsbook', 'live casino', 'rtp slot',
            'bocoran slot', 'pola slot', 'jam gacor', 'link alternatif', 'daftar slot',
            'akun pro', 'akun demo', 'freespin', 'bonus new member', 'turnover',
            'slot88', 'slot777', 'joker123', 'habanero',
            'bandar judi', 'situs judi', 'agen slot', 'agen togel', 'toto gelap',
            'prediksi hk', 'prediksi sgp', 'prediksi sydney', 'angka main', 'angka jitu',
            'colok bebas', 'colok naga',
            'slot gacor hari ini', 'rtp tertinggi', 'scatter hitam', 'wild multiplier',
            'spin gratis', 'bonus deposit', 'vip member',
            'wd cepat', 'wd lancar', 'depo pulsa', 'depo dana', 'cashback slot',
            'cashback turnover', 'referral judi', 'forex bodong', 'crypto judi'
        ],
        'obat_penguat': [
            'viagra', 'cialis', 'levitra', 'obat kuat', 'stamina pria', 'tahan lama',
            'pembesar', 'ereksi', 'vitalitas', 'libido', 'disfungsi ereksi',
            'obat perkasa', 'obat jantan', 'herbal pria', 'suplemen pria',
            'kuat pria', 'obat lelaki', 'pil biru', 'hammer of thor',
            'titan gel', 'klg pills', 'vimax', 'obat impotensi',
            'obat lemah syahwat', 'mr p', 'alat vital', 'obat loyo',
            'obat kuat dewasa'
        ],
        'obat_aborsi': [
            'obat aborsi', 'obat gugurkan', 'obat telat bulan', 'misoprostol', 'cytotec',
            'gastrul', 'obat penggugur', 'cara menggugurkan', 'gugurkan kandungan',
            'obat tuntas', 'obat ampuh gugur', 'klinik aborsi',
            'obat pelancar haid', 'terlambat datang bulan', 'obat terlambat haid',
            'gugurkan rahim', 'obat rahim aborsi', 'bersihkan rahim'
        ],
        'konten_dewasa': [
            'bokep', 'porn', 'xxx', 'sex video', 'nude', 'naked',
            'onlyfans', 'cam girl', 'escort', 'pijat plus', 'spa plus',
            'open bo', 'open vcs', 'jasa pijat', 'massage plus',
        ],
        'penipuan': [
            'pinjol', 'pinjaman online', 'kredit tanpa jaminan', 'dana cepat',
            'investasi bodong', 'money game', 'ponzi', 'profit pasti',
            'robot trading', 'binary option', 'double profit', 'passive income',
            'kerja online', 'bisnis online', 'income jutaan', 'cara cepat kaya',
            'trading forex', 'bitcoin mining', 'crypto investment',
        ],
    }
    
    # Kata kunci konteks aman - menandakan konten positif/edukatif
    # Jika ditemukan bersama keyword negatif, confidence score akan diturunkan
    SAFE_CONTEXT_KEYWORDS = {
        'obat_aborsi': [
            'penyuluhan', 'edukasi', 'kesehatan', 'puskesmas', 'dokter', 'rumah sakit',
            'bidan', 'klinik', 'medis', 'kedokteran', 'kebidanan', 'ibu hamil',
            'kehamilan sehat', 'kandungan', 'persalinan', 'prenatal', 'postnatal',
            'kesehatan ibu', 'kesehatan reproduksi', 'pelayanan kesehatan', 'pkm',
            'dinas kesehatan', 'dinkes', 'rsud', 'poliklinik', 'posyandu',
            'artikel kesehatan', 'info kesehatan', 'tips kesehatan', 'konsultasi dokter',
        ],
        'konten_dewasa': [
            'mtq', 'musabaqah', 'quran', 'tilawah', 'agama', 'islami', 'islam',
            'hafidz', 'hafidzah', 'qori', 'qoriah', 'tadarus', 'pengajian',
            'masjid', 'musholla', 'pesantren', 'madrasah', 'pondok', 'ustadz',
            'ceramah', 'dakwah', 'kajian', 'tausiyah', 'majlis taklim',
            'ramadhan', 'idul fitri', 'idul adha', 'maulid', 'isra miraj',
            'ntb', 'nusa tenggara', 'lombok', 'mataram', 'kabupaten', 'kecamatan',
            'pemerintah', 'dinas', 'sekretariat', 'bupati', 'walikota', 'gubernur',
        ],
        'obat_penguat': [
            'kesehatan pria', 'konsultasi dokter', 'urologi', 'andrologi',
            'rumah sakit', 'klinik resmi', 'farmasi', 'apotik resmi',
            'artikel kesehatan', 'edukasi kesehatan', 'tips kesehatan',
        ],
        'judol': [
            # Untuk judol biasanya tidak ada safe context - tetap tinggi
        ],
        'penipuan': [
            # Untuk penipuan biasanya tidak ada safe context - tetap tinggi
        ],
    }
    
    def __init__(self, custom_keywords: Dict[str, List[str]] = None):
        """
        Initialize detector with optional custom keywords
        
        Args:
            custom_keywords: Dictionary dengan kategori sebagai key dan list kata kunci sebagai value
        """
        self.keywords = self.DEFAULT_KEYWORDS.copy()
        self.whitelist = []  # Initialize whitelist
        if custom_keywords:
            for category, words in custom_keywords.items():
                if category in self.keywords:
                    self.keywords[category].extend(words)
                else:
                    self.keywords[category] = words
        self.compile_patterns()
    
    def compile_patterns(self):
        """Build a single Aho-Corasick automaton for all keywords across all categories, 
        plus safe-context patterns."""
        self._keyword_casing_map = {}
        self._automaton = ahocorasick.Automaton()
        
        for category, keywords in self.keywords.items():
            for kw in keywords:
                if kw:
                    kw_lower = kw.lower()
                    self._keyword_casing_map[kw_lower] = kw
                    self._automaton.add_word(kw_lower, (category, kw_lower))
        
        self._automaton.make_automaton()
        
        # Safe-context: separate automaton (used selectively, not in two-stage pass)
        self._safe_keyword_casing_map = {}
        self._safe_automaton = ahocorasick.Automaton()
        for category, keywords in self.SAFE_CONTEXT_KEYWORDS.items():
            for kw in keywords:
                if kw:
                    kw_lower = kw.lower()
                    self._safe_keyword_casing_map[kw_lower] = kw
                    self._safe_automaton.add_word(kw_lower, (category, kw_lower))
        self._safe_automaton.make_automaton()
    
    def load_keywords_from_db(self, keyword_queryset):
        """
        Load kata kunci dari database
        
        Args:
            keyword_queryset: QuerySet dari model Keyword
        """
        for kw in keyword_queryset:
            if kw.category in self.keywords:
                if kw.keyword.lower() not in [k.lower() for k in self.keywords[kw.category]]:
                    self.keywords[kw.category].append(kw.keyword.lower())
            else:
                self.keywords[kw.category] = [kw.keyword.lower()]
        self.compile_patterns()
    
    def load_whitelist_from_db(self, whitelist_queryset):
        """
        Load daftar whitelist dari database
        
        Args:
            whitelist_queryset: QuerySet dari model Whitelist (yang aktif)
        """
        self.whitelist = []
        for wl in whitelist_queryset:
            self.whitelist.append({
                'url': wl.url,
                'domain': wl.domain,
                'keyword': wl.keyword.lower() if wl.keyword else '',
                'type': wl.whitelist_type,
            })
    
    def is_whitelisted(self, url: str, keyword: str = None) -> bool:
        """
        Cek apakah URL atau keyword tertentu di-whitelist
        
        Args:
            url: URL halaman yang sedang dicek
            keyword: Keyword yang terdeteksi (opsional)
            
        Returns:
            True jika URL/keyword di-whitelist, False jika tidak
        """
        from urllib.parse import urlparse
        parsed = urlparse(url)
        current_domain = parsed.netloc
        
        for wl in self.whitelist:
            if wl['type'] == 'domain' and wl['domain']:
                # Whitelist seluruh domain
                if current_domain == wl['domain'] or current_domain.endswith('.' + wl['domain']):
                    return True
            elif wl['type'] == 'url':
                # Whitelist URL spesifik (exact atau prefix match)
                if url == wl['url'] or url.startswith(wl['url']):
                    return True
            elif wl['type'] == 'keyword_url' and keyword:
                # Whitelist keyword tertentu di URL tertentu
                if (url == wl['url'] or url.startswith(wl['url'])) and keyword.lower() == wl['keyword']:
                    return True
        return False

    
    def detect(self, text: str, context_length: int = 100) -> List[Dict]:
        """
        Mendeteksi konten negatif dalam teks menggunakan Aho-Corasick automaton.
        Single-pass matching regardless of keyword count.
        """
        if not hasattr(self, '_automaton'):
            self.compile_patterns()
        
        detections = []
        text_lower = text.lower()
        
        for end_idx, (category, kw_lower) in self._automaton.iter(text_lower):
            start_idx = end_idx - len(kw_lower) + 1
            
            # Word boundary check (Aho-Corasick does substring matching)
            if start_idx > 0 and text_lower[start_idx - 1].isalnum():
                continue
            if end_idx + 1 < len(text_lower) and text_lower[end_idx + 1].isalnum():
                continue
            
            keyword = self._keyword_casing_map.get(kw_lower, kw_lower)
            matched_text = text[start_idx:end_idx + 1]
            
            ctx_start = max(0, start_idx - context_length)
            ctx_end = min(len(text), end_idx + 1 + context_length)
            context = text[ctx_start:ctx_end]
            
            if ctx_start > 0:
                context = '...' + context
            if ctx_end < len(text):
                context = context + '...'
            
            detections.append({
                'keyword': keyword,
                'matched_text': matched_text,
                'category': category,
                'context': context,
                'position': start_idx,
            })
        
        return detections
    
    def find_safe_context(self, text: str, category: str) -> List[str]:
        """
        Mencari kata-kata konteks aman dalam teks menggunakan Aho-Corasick.
        Filtered by category after matching.
        """
        if not hasattr(self, '_safe_automaton'):
            self.compile_patterns()
        
        text_lower = text.lower()
        matches = []
        
        for end_idx, (match_category, kw_lower) in self._safe_automaton.iter(text_lower):
            if match_category != category:
                continue
            start_idx = end_idx - len(kw_lower) + 1
            
            # Word boundary check
            if start_idx > 0 and text_lower[start_idx - 1].isalnum():
                continue
            if end_idx + 1 < len(text_lower) and text_lower[end_idx + 1].isalnum():
                continue
            
            keyword = self._safe_keyword_casing_map.get(kw_lower, kw_lower)
            if keyword not in matches:
                matches.append(keyword)
        
        return matches
    
    # Keyword strength tiers — maps category -> keyword -> strength
    # decisive: single match is high confidence (e.g. misoprostol, slot gacor)
    # strong:   needs 1 corroborating keyword
    # weak:     needs >=3 corroborating keywords or a decisive one
    DEFAULT_KEYWORD_STRENGTHS = {
        'judol': {
            'slot gacor': 'decisive', 'maxwin': 'decisive', 'rtp live': 'decisive',
            'pragmatic': 'decisive', 'pg soft': 'decisive', 'mahjong ways': 'decisive',
            'sweet bonanza': 'decisive', 'gates of olympus': 'decisive',
            'starlight princess': 'decisive', 'wild west gold': 'decisive',
            'slot88': 'decisive', 'slot777': 'decisive', 'joker123': 'decisive',
            'habanero': 'decisive', 'slot gacor hari ini': 'decisive',
            'rtp tertinggi': 'decisive', 'scatter hitam': 'decisive',
            'wild multiplier': 'decisive', 'bandar togel': 'decisive',
            'bandar judi': 'decisive', 'situs judi': 'decisive',
            'agen slot': 'decisive', 'agen togel': 'decisive', 'toto gelap': 'decisive',
            'link alternatif': 'decisive', 'daftar slot': 'decisive',
            'bocoran slot': 'decisive', 'pola slot': 'decisive', 'jam gacor': 'decisive',
            'freespin': 'decisive', 'bonus new member': 'decisive',
            'prediksi hk': 'decisive', 'prediksi sgp': 'decisive',
            'prediksi sydney': 'decisive', 'colok bebas': 'decisive',
            'colok naga': 'decisive', 'wd cepat': 'decisive', 'wd lancar': 'decisive',
            'depo pulsa': 'decisive', 'depo dana': 'decisive', 'cashback slot': 'decisive',
            'cashback turnover': 'decisive', 'referral judi': 'decisive',
            'forex bodong': 'decisive', 'crypto judi': 'decisive',
            'slot online': 'strong', 'judi online': 'strong', 'taruhan': 'strong',
            'betting': 'strong', 'bonus deposit': 'strong', 'deposit pulsa': 'strong',
            'spin gratis': 'strong', 'akun pro': 'strong', 'akun demo': 'strong',
            'vip member': 'strong', 'rtp slot': 'strong', 'live casino': 'strong',
            'sportsbook': 'strong', 'bandar bola': 'strong',
            'slot': 'weak', 'togel': 'weak', 'poker': 'weak', 'casino': 'weak',
            'jackpot': 'weak', 'scatter': 'weak', 'olympus': 'weak', 'turnover': 'weak',
            'gacor': 'weak',
        },
        'obat_aborsi': {
            'misoprostol': 'decisive', 'cytotec': 'decisive', 'gastrul': 'decisive',
            'obat aborsi': 'decisive', 'obat penggugur': 'decisive',
            'klinik aborsi': 'decisive', 'obat gugurkan': 'decisive',
            'obat telat bulan': 'decisive', 'cara menggugurkan': 'decisive',
            'gugurkan kandungan': 'decisive', 'obat tuntas': 'decisive',
            'obat ampuh gugur': 'decisive', 'gugurkan rahim': 'decisive',
            'obat rahim aborsi': 'decisive', 'bersihkan rahim': 'decisive',
        },
        'obat_penguat': {
            'viagra': 'decisive', 'cialis': 'decisive', 'levitra': 'decisive',
            'hammer of thor': 'decisive', 'titan gel': 'decisive', 'klg pills': 'decisive',
            'vimax': 'decisive', 'obat kuat dewasa': 'decisive',
            'mr p': 'weak',
        },
        'konten_dewasa': {
            'bokep': 'decisive', 'onlyfans': 'decisive', 'escort': 'decisive',
            'pijat plus': 'decisive', 'spa plus': 'decisive', 'open bo': 'decisive',
            'open vcs': 'decisive',
            'xxx': 'weak', 'nude': 'weak', 'naked': 'weak',
        },
        'penipuan': {
            'pinjol': 'decisive', 'money game': 'decisive', 'ponzi': 'decisive',
            'binary option': 'decisive', 'robot trading': 'decisive',
            'investasi bodong': 'decisive', 'profit pasti': 'decisive',
            'double profit': 'decisive', 'pinjaman online': 'decisive',
            'kredit tanpa jaminan': 'decisive',
        },
    }

    def calculate_confidence_score(
        self,
        detections: List[Dict],
        full_text: str,
        title: str = '',
        url: str = '',
        outbound_links: List[str] = None,
        keyword_strengths: Dict[str, str] = None,
        has_cloaking: bool = False,
        cloaked_snippets: List[str] = None,
    ) -> Tuple[float, List[str]]:
        """
        Menghitung confidence score berdasarkan evidence aggregation.

        Builds from 0.0 — a single weak match without supporting evidence stays low.
        """
        if not detections:
            return 0.0, []

        score = 0.0
        all_safe_context = []
        categories = set(d['category'] for d in detections)

        # Collect safe context per category
        for category in categories:
            safe_words = self.find_safe_context(full_text, category)
            all_safe_context.extend(safe_words)

        # ---- Factor 1: Keyword strength tier ----
        strengths_found = set()
        strengths = keyword_strengths
        if strengths is None:
            strengths = {}
            for cat, kw_map in self.DEFAULT_KEYWORD_STRENGTHS.items():
                strengths.update(kw_map)
        
        for d in detections:
            s = strengths.get(d['keyword'].lower())
            if s:
                strengths_found.add(s)

        if 'decisive' in strengths_found:
            score += 0.60
        elif 'strong' in strengths_found:
            score += 0.35
        # weak only: no strength bonus (stays near 0)

        # ---- Factor 2: Keyword diversity ----
        unique_keywords = set(d['keyword'].lower() for d in detections)
        if len(unique_keywords) >= 3:
            score += 0.20
        elif len(unique_keywords) >= 2:
            score += 0.10

        # ---- Factor 3: Proximity clustering ----
        positions = sorted(d.get('position', 0) for d in detections if d.get('position') is not None)
        has_cluster = False
        if len(positions) >= 3:
            for i in range(len(positions) - 2):
                if positions[i + 2] - positions[i] <= 300:
                    has_cluster = True
                    break
        if has_cluster:
            score += 0.15

        # ---- Factor 4: Location (title / meta) ----
        locations = set(d.get('location', 'content') for d in detections)
        if 'title' in locations:
            score += 0.15
        if 'meta' in locations:
            score += 0.10

        # ---- Factor 5: Keyword density ----
        num_detections = len(detections)
        text_length = len(full_text)
        if text_length > 0:
            density = num_detections / text_length
            if density > 0.002:   # > 2 hits per 1000 chars
                score += 0.10

        # ---- Factor 6: Suspicious outbound links ----
        has_suspicious_outbound = False
        if outbound_links:
            suspicious_patterns = re.compile(
                r'slot|togel|gacor|judi|bola|maxwin|casino|poker|betting|'
                r'\.xyz\b|\.online\b|\.win\b|\.bet\b|\.live\b|\.cc\b|\.top\b|\.vip\b|\.site\b',
                re.IGNORECASE,
            )
            for link in outbound_links:
                parsed_link = urlparse(link)
                link_host = parsed_link.netloc.lower()
                link_path = parsed_link.path.lower()
                if suspicious_patterns.search(link_host) or suspicious_patterns.search(link_path):
                    has_suspicious_outbound = True
                    break

        if has_suspicious_outbound:
            if url and '.go.id' in url.lower():
                score = max(score, 0.98)
            else:
                score += 0.25

        # ---- Factor 7: Safe-context dampening ----
        num_safe = len(set(all_safe_context))
        if num_safe > 0:
            score -= min(num_safe * 0.08, 0.30)

        # ---- Factor 8: Institution / news dampening ----
        dampen = False
        if categories.intersection({'obat_aborsi', 'obat_penguat'}):
            health_institutions = [
                'puskesmas', 'rsud', 'rumah sakit', 'dinas kesehatan',
                'posyandu', 'kemenkes', 'sehat', 'health',
            ]
            news_contexts = [
                'berita', 'artikel', 'info', 'news', 'edukasi',
                'penyuluhan', 'sosialisasi',
            ]
            title_lower = title.lower()
            url_lower = url.lower()
            for kw in health_institutions + news_contexts:
                if kw in title_lower or kw in url_lower:
                    dampen = True
                    break
        if dampen:
            score -= 0.25

        # ---- Factor 9: Government domain (non-judol/penipuan categories) ----
        if url and '.go.id' in url.lower():
            if 'judol' not in categories and 'penipuan' not in categories:
                score -= 0.15

        # ---- Factor 10: Hidden/cloaked content boost ----
        if has_cloaking:
            # Check if any detections are inside cloaked regions
            if cloaked_snippets:
                for snippet in cloaked_snippets:
                    snippet_lower = snippet.lower()
                    for d in detections:
                        if d['keyword'].lower() in snippet_lower:
                            score += 0.20
                            break
            # General cloaking boost for any detection on a cloaked page
            score += 0.10

        score = max(0.0, min(1.0, score))
        return round(score, 2), list(set(all_safe_context))
    
    def has_any_hits(self, raw_text: str) -> bool:
        """
        Stage A (two-stage detection): quick check if raw response body has any keyword hits.
        Runs the cheap Aho-Corasick pass; returns True if any keyword found, False otherwise.
        """
        if not hasattr(self, '_automaton'):
            self.compile_patterns()
        text_lower = raw_text.lower()
        for end_idx, (category, kw_lower) in self._automaton.iter(text_lower):
            start_idx = end_idx - len(kw_lower) + 1
            if start_idx > 0 and text_lower[start_idx - 1].isalnum():
                continue
            if end_idx + 1 < len(text_lower) and text_lower[end_idx + 1].isalnum():
                continue
            return True
        return False

    def detect_in_sections(self, title: str, meta_description: str, content: str, url: str = '', outbound_links: List[str] = None, keyword_strengths: Dict[str, str] = None, has_cloaking: bool = False, cloaked_snippets: List[str] = None) -> Tuple[List[Dict], float, List[str]]:
        """
        Mendeteksi konten negatif di berbagai bagian halaman

        Args:
            title: Judul halaman
            meta_description: Meta description halaman
            content: Konten utama halaman
            url: URL halaman (untuk analisis konteks)
            outbound_links: List URL outbound eksternal dari halaman ini
            keyword_strengths: Dict mapping keyword (lowercase) -> strength tier

        Returns:
            Tuple: (list deteksi, confidence_score, list safe_context_found)
        """
        all_detections = []

        # Detect in title
        title_detections = self.detect(title, context_length=50)
        for d in title_detections:
            d['location'] = 'title'
            d['severity'] = 'high'  # Konten negatif di title lebih serius
            all_detections.append(d)

        # Detect in meta description
        meta_detections = self.detect(meta_description, context_length=100)
        for d in meta_detections:
            d['location'] = 'meta'
            d['severity'] = 'high'
            all_detections.append(d)

        # Detect in content
        content_detections = self.detect(content, context_length=150)
        for d in content_detections:
            d['location'] = 'content'
            d['severity'] = 'medium'
            all_detections.append(d)

        # Hitung confidence score berdasarkan konteks
        full_text = f"{title} {meta_description} {content}"
        confidence_score, safe_context = self.calculate_confidence_score(
            all_detections, full_text, title, url,
            outbound_links=outbound_links, keyword_strengths=keyword_strengths,
            has_cloaking=has_cloaking, cloaked_snippets=cloaked_snippets,
        )

        # Tambahkan confidence ke setiap deteksi
        for d in all_detections:
            d['confidence_score'] = confidence_score
            d['safe_context_found'] = safe_context

        return all_detections, confidence_score, safe_context
    
    def get_summary(self, detections: List[Dict]) -> Dict:
        """
        Mendapatkan ringkasan hasil deteksi
        
        Args:
            detections: List hasil deteksi
            
        Returns:
            Dictionary berisi ringkasan per kategori
        """
        summary = {}
        for detection in detections:
            category = detection['category']
            if category not in summary:
                summary[category] = {
                    'count': 0,
                    'keywords': set(),
                    'severity_counts': {'low': 0, 'medium': 0, 'high': 0}
                }
            summary[category]['count'] += 1
            summary[category]['keywords'].add(detection['keyword'])
            severity = detection.get('severity', 'medium')
            summary[category]['severity_counts'][severity] += 1
        
        # Convert sets to lists for JSON serialization
        for category in summary:
            summary[category]['keywords'] = list(summary[category]['keywords'])
        
        return summary
