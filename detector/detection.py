"""
Content Detector - Engine untuk mendeteksi konten negatif
"""
import re
from typing import List, Dict, Tuple


class ContentDetector:
    """Kelas untuk mendeteksi konten negatif berdasarkan kata kunci"""
    
    # Kata kunci default
    #
    # Catatan: beberapa keyword generik (`4d`, `3d`, `2d`, `wd`, `depo`, `ekor`,
    # `shio`, `withdraw`, `cashback`, `referral`, `forex`, `mr p`) telah dihapus
    # untuk menekan false positive di konten resmi (contoh "3D printing",
    # "ekor pesawat", "cashback BPJS", "referral pasien"). Variannya yang
    # spesifik untuk judol (`slot gacor`, `link alternatif`, `bonus new member`,
    # `bonus deposit`, dll.) tetap menangkap halaman judol asli.
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
        ],
        'obat_penguat': [
            'viagra', 'cialis', 'levitra', 'obat kuat', 'stamina pria', 'tahan lama',
            'pembesar', 'ereksi', 'vitalitas', 'libido', 'disfungsi ereksi',
            'obat perkasa', 'obat jantan', 'herbal pria', 'suplemen pria',
            'kuat pria', 'obat lelaki', 'pil biru', 'hammer of thor',
            'titan gel', 'klg pills', 'vimax', 'obat impotensi',
            'obat lemah syahwat', 'alat vital', 'obat loyo',
        ],
        'obat_aborsi': [
            'obat aborsi', 'obat gugurkan', 'obat telat bulan', 'misoprostol', 'cytotec',
            'gastrul', 'obat penggugur', 'cara menggugurkan', 'gugurkan kandungan',
            'rahim', 'obat tuntas', 'obat ampuh gugur', 'klinik aborsi',
            'obat pelancar haid', 'terlambat datang bulan', 'obat terlambat haid',
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
        # deep-copy the lists so callers don't accidentally mutate the class-level default
        self.keywords: Dict[str, List[str]] = {
            cat: list(words) for cat, words in self.DEFAULT_KEYWORDS.items()
        }
        self.whitelist = []  # Initialize whitelist
        if custom_keywords:
            for category, words in custom_keywords.items():
                if category in self.keywords:
                    self.keywords[category].extend(words)
                else:
                    self.keywords[category] = list(words)

        # Pre-compiled per-category alternation regex (built lazily).
        # Built on first call to detect() / find_safe_context() and rebuilt
        # whenever keywords are loaded from the DB.
        self._compiled_keywords: Dict[str, re.Pattern] = {}
        self._compiled_safe_context: Dict[str, re.Pattern] = {}
        self._compile_patterns()

    @staticmethod
    def _build_alternation(words) -> re.Pattern:
        """Build a single case-insensitive, word-bounded alternation regex.

        Longer phrases are emitted first so e.g. ``slot gacor hari ini`` matches
        before the shorter ``slot``. Empty/whitespace-only entries are skipped.
        """
        cleaned = sorted({w.strip() for w in words if w and w.strip()}, key=len, reverse=True)
        if not cleaned:
            return None
        pattern = r'\b(?:' + '|'.join(re.escape(w) for w in cleaned) + r')\b'
        return re.compile(pattern, re.IGNORECASE)

    def _compile_patterns(self) -> None:
        """(Re)compile the per-category keyword and safe-context regexes."""
        self._compiled_keywords = {
            cat: pat
            for cat, words in self.keywords.items()
            if (pat := self._build_alternation(words)) is not None
        }
        self._compiled_safe_context = {
            cat: pat
            for cat, words in self.SAFE_CONTEXT_KEYWORDS.items()
            if (pat := self._build_alternation(words)) is not None
        }

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
        # Patterns are stale now; rebuild before next detection run.
        self._compile_patterns()
    
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
        Mendeteksi konten negatif dalam teks
        
        Args:
            text: Teks yang akan diperiksa
            context_length: Panjang konteks di sekitar kata kunci yang ditemukan
            
        Returns:
            List of dictionaries berisi informasi deteksi
        """
        if not text:
            return []

        detections: List[Dict] = []
        text_len = len(text)

        # One compiled alternation pattern per category. We rely on
        # ``re.IGNORECASE`` instead of lowercasing the whole text, which keeps
        # ``matched_text`` faithful to the original casing in the source.
        for category, pattern in self._compiled_keywords.items():
            for match in pattern.finditer(text):
                matched_text = match.group(0)
                start = max(0, match.start() - context_length)
                end = min(text_len, match.end() + context_length)
                context = text[start:end]
                if start > 0:
                    context = '...' + context
                if end < text_len:
                    context = context + '...'

                detections.append({
                    'keyword': matched_text.lower(),
                    'matched_text': matched_text,
                    'category': category,
                    'context': context,
                    'position': match.start(),
                })

        return detections
    
    def find_safe_context(self, text: str, category: str) -> List[str]:
        """
        Mencari kata-kata konteks aman dalam teks (dengan word boundary).

        Menggunakan regex `\b...\b` agar tidak terjadi false-match seperti
        "kandungan" terbaca di dalam "berkandungan".
        """
        if not text:
            return []
        pattern = self._compiled_safe_context.get(category)
        if pattern is None:
            return []
        # de-duplicate while preserving discovery order
        found = []
        seen = set()
        for match in pattern.finditer(text):
            word = match.group(0).lower()
            if word not in seen:
                seen.add(word)
                found.append(word)
        return found
    
    def calculate_confidence_score(
        self, 
        detections: List[Dict], 
        full_text: str, 
        title: str = '', 
        url: str = ''
    ) -> Tuple[float, List[str]]:
        """
        Menghitung confidence score berdasarkan konteks
        
        Args:
            detections: List hasil deteksi di halaman ini
            full_text: Seluruh teks halaman (title + meta + content)
            title: Judul halaman
            url: URL halaman
            
        Returns:
            Tuple: (confidence_score 0.0-1.0, list safe_context_found)
        """
        if not detections:
            return 0.0, []
        
        # Base score dimulai dari 1.0 (paling tinggi)
        score = 1.0
        all_safe_context = []
        
        # Kumpulkan semua kategori yang terdeteksi
        categories = set(d['category'] for d in detections)
        
        # Cek safe context untuk setiap kategori
        for category in categories:
            safe_words = self.find_safe_context(full_text, category)
            all_safe_context.extend(safe_words)
        
        # Faktor 1: Jumlah safe context ditemukan
        # Lebih banyak safe context = score lebih rendah
        num_safe = len(set(all_safe_context))  # unique safe words
        if num_safe > 0:
            # Setiap safe word mengurangi score 0.1, maksimal 0.5 reduksi
            reduction = min(num_safe * 0.1, 0.5)
            score -= reduction
        
        # Faktor 2: Rasio keyword negatif vs panjang teks
        # Jika hanya 1 keyword di teks panjang = kemungkinan false positive
        num_detections = len(detections)
        text_length = len(full_text)
        
        if text_length > 1000 and num_detections <= 2:
            # Teks panjang dengan sedikit deteksi = mungkin false positive
            score -= 0.15
        
        # Faktor 3: Lokasi deteksi
        # Jika TIDAK ada di title/meta (hanya di content) = sedikit lebih rendah
        locations = set(d.get('location', 'content') for d in detections)
        if 'title' not in locations and 'meta' not in locations:
            score -= 0.1
        
        # Faktor 4: Kategori tertentu lebih mungkin false positive
        # obat_aborsi dan konten_dewasa lebih rentan false positive
        high_fp_categories = {'obat_aborsi', 'konten_dewasa', 'obat_penguat'}
        if categories.issubset(high_fp_categories) and num_safe > 0:
            score -= 0.1
        
        # Faktor 5: Cek URL untuk domain pemerintah dengan kategori non-judol
        if url and '.go.id' in url.lower():
            if 'judol' not in categories and 'penipuan' not in categories:
                # Domain pemerintah dengan kategori kesehatan = mungkin false positive
                score -= 0.1
        
        # Faktor 6: Cek title untuk kata-kata institusi kesehatan
        health_institutions = ['puskesmas', 'rsud', 'rumah sakit', 'dinas kesehatan', 'posyandu']
        title_lower = title.lower()
        for inst in health_institutions:
            if inst in title_lower:
                score -= 0.15
                break
        
        # Pastikan score dalam range 0.0 - 1.0
        score = max(0.0, min(1.0, score))
        
        return round(score, 2), list(set(all_safe_context))
    
    def detect_in_sections(self, title: str, meta_description: str, content: str, url: str = '') -> Tuple[List[Dict], float, List[str]]:
        """
        Mendeteksi konten negatif di berbagai bagian halaman
        
        Args:
            title: Judul halaman
            meta_description: Meta description halaman
            content: Konten utama halaman
            url: URL halaman (untuk analisis konteks)
            
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
            all_detections, full_text, title, url
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
