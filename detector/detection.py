"""
Content Detector - Engine untuk mendeteksi konten negatif
"""
import re
from typing import List, Dict, Tuple


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
            'withdraw', 'depo', 'wd', 'slot88', 'slot777', 'joker123', 'habanero',
            'bandar judi', 'situs judi', 'agen slot', 'agen togel', 'toto gelap',
            'prediksi hk', 'prediksi sgp', 'prediksi sydney', 'angka main', 'angka jitu',
            'colok bebas', 'colok naga', '4d', '3d', '2d', 'shio', 'ekor',
            'slot gacor hari ini', 'rtp tertinggi', 'scatter hitam', 'wild multiplier',
            'spin gratis', 'bonus deposit', 'cashback', 'referral', 'vip member',
        ],
        'obat_penguat': [
            'viagra', 'cialis', 'levitra', 'obat kuat', 'stamina pria', 'tahan lama',
            'pembesar', 'ereksi', 'vitalitas', 'libido', 'disfungsi ereksi',
            'obat perkasa', 'obat jantan', 'herbal pria', 'suplemen pria',
            'kuat pria', 'obat lelaki', 'obat dewasa', 'pil biru', 'hammer of thor',
            'titan gel', 'klg pills', 'forex', 'vimax', 'obat impotensi',
            'obat lemah syahwat', 'mr p', 'alat vital', 'obat loyo',
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
    
    def __init__(self, custom_keywords: Dict[str, List[str]] = None):
        """
        Initialize detector with optional custom keywords
        
        Args:
            custom_keywords: Dictionary dengan kategori sebagai key dan list kata kunci sebagai value
        """
        self.keywords = self.DEFAULT_KEYWORDS.copy()
        if custom_keywords:
            for category, words in custom_keywords.items():
                if category in self.keywords:
                    self.keywords[category].extend(words)
                else:
                    self.keywords[category] = words
    
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
    
    def detect(self, text: str, context_length: int = 100) -> List[Dict]:
        """
        Mendeteksi konten negatif dalam teks
        
        Args:
            text: Teks yang akan diperiksa
            context_length: Panjang konteks di sekitar kata kunci yang ditemukan
            
        Returns:
            List of dictionaries berisi informasi deteksi
        """
        detections = []
        text_lower = text.lower()
        
        for category, keywords in self.keywords.items():
            for keyword in keywords:
                keyword_lower = keyword.lower()
                # Gunakan regex untuk word boundary matching
                pattern = r'\b' + re.escape(keyword_lower) + r'\b'
                
                for match in re.finditer(pattern, text_lower):
                    start = max(0, match.start() - context_length)
                    end = min(len(text), match.end() + context_length)
                    context = text[start:end]
                    
                    # Tambahkan penanda awal dan akhir konteks
                    if start > 0:
                        context = '...' + context
                    if end < len(text):
                        context = context + '...'
                    
                    detections.append({
                        'keyword': keyword,
                        'matched_text': text[match.start():match.end()],
                        'category': category,
                        'context': context,
                        'position': match.start(),
                    })
        
        return detections
    
    def detect_in_sections(self, title: str, meta_description: str, content: str) -> List[Dict]:
        """
        Mendeteksi konten negatif di berbagai bagian halaman
        
        Args:
            title: Judul halaman
            meta_description: Meta description halaman
            content: Konten utama halaman
            
        Returns:
            List of dictionaries berisi informasi deteksi dengan lokasi
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
        
        return all_detections
    
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
