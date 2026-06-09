import pdfplumber
import pandas as pd
import re
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# --- 配置與常數設定 (Configuration) ---

class Config:
    # 檔案路徑
    PDF_FILES = [
        Path("0sl400elibeydt454p2123un (1).pdf"), # 桃捷
        Path("0sl400elibeydt454p2123un.pdf"),     # 北捷/環狀
        Path("0sl400elibeydt454p2123un (2).pdf")  # 台鐵
    ]
    
    FARE_DB_FILES = {
        'TRA': Path('TRA.json'),
        'TRTC': Path('TRTC.xml'),
        'TYMC': Path('TYMC.xml'),
        'NTMC': Path('NTMC.xml')
    }
    
    OUTPUT_FILE = Path('final_fare_result.csv')

    # 同站進出費率
    SAME_STATION_FEE = {
        'TRA': 22,
        'DEFAULT': 20
    }

    # 手動票價補丁 (處理跨系統或缺失資料)
    MANUAL_PATCHES: Dict[Tuple[str, str], int] = {
        ('南勢角', '幸福'): 40,
        ('幸福', '南勢角'): 40,
        ('劍潭', '新北產業園區'): 45,
        ('新北產業園區', '劍潭'): 45,
        ('公館', '景平'): 30,
        ('景平', '公館'): 30,
    }

    # 站名標準化：強制對應表
    STATION_MAPPING = {
        '台北二': '台北',
        '臺北': '台北'
    }

    # --- Regex 優化區 ---
    
    # 1. 雜訊清洗：只移除 空白、引號、冒號、逗號 (保留 數字、英文、/、中文)
    RE_REMOVE_NOISE = re.compile(r'[\s"\'：:,]')

    # 2. 站名標準化：移除前綴
    RE_PREFIX = re.compile(r'^(名稱|進站|出站|Station)')
    
    # 3. 站名標準化：後綴截斷 (遇到這些詞就切斷)
    RE_SUFFIX_CUT = re.compile(r'(收費|金額|交易|扣款|票價|餘額|Entry|Exit|Purchase)')
    
    # 4. 站名標準化：移除殘留的英數 (用於最後階段，不要太早用)
    RE_REMOVE_ALPHANUM = re.compile(r'[a-zA-Z0-9]')


# 設定日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# --- 核心邏輯層 (Core Logic) ---

class StationNormalizer:
    """處理站名清洗與標準化的專責類別"""
    
    @staticmethod
    def clean_text_safe(text: str) -> str:
        """安全清洗：保留日期所需的數字與斜線"""
        if not text: return ""
        # 只移除定義好的雜訊符號
        return Config.RE_REMOVE_NOISE.sub('', text)

    @classmethod
    def normalize(cls, name: str) -> str:
        """將站名轉換為標準格式"""
        if not name: return ""
        
        # 1. 基礎清理 (移除冒號引號等)
        name = Config.RE_REMOVE_NOISE.sub('', name)
        
        # 2. 移除前綴 (Regex)
        name = Config.RE_PREFIX.sub('', name)
        
        # 3. 關鍵字截斷 (Split)
        match = Config.RE_SUFFIX_CUT.search(name)
        if match:
            name = name[:match.start()]
            
        # 4. 移除英文與數字 (最後階段才做，避免誤刪)
        # 這裡我們只保留中文，因為 PDF 中的站名標準化後應該只剩中文
        name = Config.RE_REMOVE_ALPHANUM.sub('', name)
        
        # 5. 統一用詞
        name = name.replace('臺', '台').replace('車站', '')
        
        # 6. 移除結尾的 '站' (若長度大於1)
        if len(name) > 1 and name.endswith('站'):
            name = name[:-1]
            
        # 7. 強制對應表修正
        return Config.STATION_MAPPING.get(name, name.strip())


class FareDatabase:
    """負責載入與查詢票價的資料庫類別"""
    
    def __init__(self):
        self._db: Dict[str, Dict[Tuple[str, str], int]] = {
            'TRA': {}, 'TRTC': {}, 'TYMC': {}, 'NTMC': {}
        }
        self._load_data()

    def _load_json_safe(self, path: Path) -> List[Dict]:
        """安全載入 JSON/XML 內容"""
        if not path.exists():
            # logger.warning(f"找不到檔案: {path}") # 避免洗版
            return []
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                content = f.read().strip()
                if content.startswith(('[', '{')):
                    return json.loads(content)
        except Exception as e:
            logger.error(f"讀取 {path} 失敗: {e}")
        return []

    def _load_data(self):
        logger.info("正在載入票價資料庫...")
        
        # 1. 載入台鐵 (TRA)
        tra_data = self._load_json_safe(Config.FARE_DB_FILES['TRA'])
        c = 0
        for item in tra_data:
            try:
                s = StationNormalizer.normalize(item['OriginStationName']['Zh_tw'])
                e = StationNormalizer.normalize(item['DestinationStationName']['Zh_tw'])
                fares = item.get('Fares', [])
                price = 0
                for t in ['成復', '成普', '成自']:
                    found = next((f for f in fares if f['TicketType'] == t), None)
                    if found:
                        price = found['Price']
                        break
                if price > 0:
                    self._db['TRA'][(s, e)] = price
                    c += 1
            except: continue
        logger.info(f">> 台鐵 (TRA) 載入: {c} 筆")

        # 2. 載入捷運系統
        for sys_key in ['TRTC', 'TYMC', 'NTMC']:
            data = self._load_json_safe(Config.FARE_DB_FILES[sys_key])
            c = 0
            for item in data:
                try:
                    s = StationNormalizer.normalize(item['OriginStationName']['Zh_tw'])
                    e = StationNormalizer.normalize(item['DestinationStationName']['Zh_tw'])
                    price_obj = next((f for f in item.get('Fares', []) if str(f.get('FareClass')) == '1'), None)
                    if price_obj:
                        self._db[sys_key][(s, e)] = price_obj['Price']
                        c += 1
                except: continue
            logger.info(f">> {sys_key} 載入: {c} 筆")

    def get_price(self, system: str, start: str, end: str) -> int:
        if not start or not end: return 0
            
        # 同站進出
        if start == end:
            return Config.SAME_STATION_FEE['TRA'] if system == 'TRA' else Config.SAME_STATION_FEE['DEFAULT']

        # 手動補丁
        if (start, end) in Config.MANUAL_PATCHES: return Config.MANUAL_PATCHES[(start, end)]
        if (end, start) in Config.MANUAL_PATCHES: return Config.MANUAL_PATCHES[(end, start)]

        # 決定查詢對象
        targets = []
        if system == 'TRA': targets = ['TRA']
        elif system == 'TYMC': targets = ['TYMC']
        elif system == 'TRTC_MIXED': targets = ['TRTC', 'NTMC']
        else: targets = [system] if system in self._db else []

        # 查表
        for db_name in targets:
            sub_db = self._db.get(db_name, {})
            price = sub_db.get((start, end)) or sub_db.get((end, start))
            if price: return price
        
        return 0


class PDFParser:
    """PDF 解析策略類別"""
    
    RE_DATE = re.compile(r'(\d{4}/\d{2}/\d{2})')
    RE_TRA_STATION = re.compile(r'臺鐵(.+?)車站')
    # 允許前面有'名稱'，允許中間有數字(如果有)，最後要 normalized 去掉
    RE_TY_STATION = re.compile(r'(?:名稱)?([\u4e00-\u9fa50-9]{2,10}站)')
    
    # 混合系統斷點
    RE_MIXED_TRIP = re.compile(
        r'進站名稱(?:EntryStation)?(.*?)出站名稱(?:ExitStation)?(.*?)(?:車資|票價|收費|金額|單程票|電子票證|交易|餘額|\d{1,3}元|\d|$)'
    )

    @staticmethod
    def extract_trips(pdf_path: Path) -> List[Dict[str, str]]:
        if not pdf_path.exists():
            return []
            
        full_text = ""
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text: full_text += text + "\n"
        except Exception as e:
            logger.error(f"解析 PDF 失敗 {pdf_path}: {e}")
            return []

        # 根據關鍵字分流
        if "桃園大眾捷運" in full_text:
            return PDFParser._parse_ty(full_text)
        elif "國營臺灣鐵路" in full_text:
            return PDFParser._parse_tra(full_text)
        elif "臺北大眾捷運" in full_text or "TRTC" in full_text:
            return PDFParser._parse_mixed(full_text)
        
        return []

    @staticmethod
    def _parse_tra(text: str) -> List[Dict]:
        # 使用 safe clean 保留日期
        clean_text = StationNormalizer.clean_text_safe(text)
        dates = PDFParser.RE_DATE.findall(clean_text)
        stations = PDFParser.RE_TRA_STATION.findall(clean_text)
        
        trips = []
        n = min(len(dates), len(stations) // 2)
        for i in range(n):
            trips.append({
                'date': dates[i],
                'system': 'TRA',
                'start': StationNormalizer.normalize(stations[i*2]),
                'end': StationNormalizer.normalize(stations[i*2+1])
            })
        return trips

    @staticmethod
    def _parse_ty(text: str) -> List[Dict]:
        clean_text = StationNormalizer.clean_text_safe(text)
        dates = PDFParser.RE_DATE.findall(clean_text)
        raw_stations = PDFParser.RE_TY_STATION.findall(clean_text)
        
        real_stations = []
        ignore_set = {'進站', '出站', '網站', '本站'}
        for rs in raw_stations:
            # 簡單過濾
            if not any(x in rs for x in ignore_set):
                real_stations.append(StationNormalizer.normalize(rs))
            
        trips = []
        n = min(len(dates), len(real_stations) // 2)
        for i in range(n):
            trips.append({
                'date': dates[i],
                'system': 'TYMC',
                'start': real_stations[i*2],
                'end': real_stations[i*2+1]
            })
        return trips

    @staticmethod
    def _parse_mixed(text: str) -> List[Dict]:
        clean_text = StationNormalizer.clean_text_safe(text)
        dates = PDFParser.RE_DATE.findall(clean_text)
        matches = PDFParser.RE_MIXED_TRIP.findall(clean_text)
        
        trips = []
        n = min(len(dates), len(matches))
        for i in range(n):
            s, e = matches[i]
            trips.append({
                'date': dates[i],
                'system': 'TRTC_MIXED',
                'start': StationNormalizer.normalize(s),
                'end': StationNormalizer.normalize(e)
            })
        return trips


# --- 主程式流程 (Main Execution) ---

def main():
    fare_db = FareDatabase()
    
    all_trips = []
    for pdf_file in Config.PDF_FILES:
        if pdf_file.exists():
            logger.info(f"處理檔案: {pdf_file.name}")
            trips = PDFParser.extract_trips(pdf_file)
            logger.info(f"  -> 提取到 {len(trips)} 筆行程")
            all_trips.extend(trips)
        
    if not all_trips:
        logger.warning("未提取到任何行程，請檢查 PDF 內容或 Regex 設定。")
        return

    df = pd.DataFrame(all_trips)
    logger.info("開始計算票價...")
    
    df['original_fare'] = df.apply(
        lambda row: fare_db.get_price(row['system'], row['start'], row['end']), 
        axis=1
    )

    df = df.sort_values(by=['date', 'system'])
    
    print("\n" + "="*80)
    print(f"{'日期':<12} {'系統':<10} {'起點':<12} {'終點':<12} {'原價'}")
    print("="*80)
    
    for _, row in df.iterrows():
        sys_display = "TRTC/NTMC" if row['system'] == 'TRTC_MIXED' else row['system']
        print(f"{row['date']:<12} {sys_display:<10} {row['start']:<12} {row['end']:<12} {row['original_fare']}")
        
    total_amount = df['original_fare'].sum()
    print("="*80)
    print(f"總金額: {total_amount} 元")
    print(f"總筆數: {len(df)}")
    
    zero_fare_trips = df[df['original_fare'] == 0]
    if not zero_fare_trips.empty:
        logger.warning(f"仍有 {len(zero_fare_trips)} 筆 0 元行程:")
        print(zero_fare_trips[['system', 'start', 'end']].to_string())
    else:
        logger.info("所有行程皆已成功取得票價。")

    df.to_csv(Config.OUTPUT_FILE, index=False, encoding='utf-8-sig')
    logger.info(f"結果已存檔至 {Config.OUTPUT_FILE}")

if __name__ == "__main__":
    main()