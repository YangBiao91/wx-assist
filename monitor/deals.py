"""Conservative, offline offer extraction. Never fetches links or executes text."""
import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PROTEIN = re.compile(r"蛋白粉|乳清|whey|protein\s*powder|(?<![A-Za-z0-9])ON\s*金标", re.I)
DEAL = re.compile(r"优惠|好价|券后|到手|实付|折扣|折后|淘礼金|低至|补贴|领券|活动价|[￥¥]|\bRMB\b|\bCNY\b|\d+(?:\.\d+)?\s*元",re.I)
URL = re.compile(r"https?://[^\s<>\"'，。]+", re.I)
CONDITIONS = (("88VIP", r"88\s*vip"), ("会员", r"会员"), ("凑单", r"凑单|满\s*\d+\s*减"),
              ("领券", r"领券|券后|优惠券|淘礼金"), ("返利", r"返利|返现|返红包"),
              ("临期", r"临期|短保|到期"), ("首单", r"首单|新客|新人"))
MERCHANTS = {"item.taobao.com", "detail.tmall.com", "item.jd.com", "mobile.yangkeduo.com"}
KEYWORD = re.compile(r"超\s*好\s*(?:[价價忦]|[讠言]\s*介)")
KEYWORD_FORMS = ("超好价","超好忦","超好價","超好讠介","超好言介")


@dataclass(frozen=True)
class Offer:
    title: str
    prices_yuan: list
    weight_g: float | None
    yuan_per_kg: float | None
    conditions: list
    link: str
    fingerprint: str
    brand: str
    threshold_yuan: int | None
    match_status: str
    reasons: list
    expiry_ts: float | None
    expiry_label: str
    trigger_kind: str = "price"
    matched_keywords: list = field(default_factory=list)
    keyword_excerpt: str = ""

    def data(self):
        return asdict(self)


def clean(text):
    text = URL.sub(" ", text)
    # Coupon passwords and contact/sender lines are not part of a product title.
    text = re.sub(r"[￥¥$][^\s￥¥$]{0,40}[A-Za-z][^\s￥¥$]{0,40}[￥¥$]", " ", text)
    text = re.sub(r"(?:复制|口令|淘口令|微信号|联系我|加微).*$", "", text, flags=re.M)
    return re.sub(r"[\x00-\x1f\x7f]", " ", text).strip()


def public_link(text):
    # Store only direct public product IDs, not shortlinks, tokens or trackers.
    for raw in URL.findall(text):
        try:
            u = urlsplit(raw)
            host = (u.hostname or "").lower()
            if host not in MERCHANTS or u.username or u.password or u.port:
                continue
            params = [(k, v) for k, v in parse_qsl(u.query)
                      if k in {"id", "goods_id", "skuId"} and re.fullmatch(r"\d{1,24}", v)]
            if not re.fullmatch(r"/[A-Za-z0-9_./-]{0,100}", u.path):
                continue
            if not params and not re.fullmatch(r"/\d{1,24}\.html", u.path):
                continue
            return urlunsplit(("https", host, u.path, urlencode(sorted(params)), ""))
        except ValueError:
            continue
    return ""


def expiry(text, reference_ts):
    base = datetime.fromtimestamp(reference_ts, timezone.utc).astimezone()
    prefix = r"(?:有效期(?:至|到)?|截止(?:到|至)?|截至|券期至|活动至|限时至)[\s:：]*"
    absolute = re.search(prefix+r"(?:(20\d{2})[年./-])?(\d{1,2})[月./-](\d{1,2})日?(?:\s*(\d{1,2})[:：](\d{2}))?", text)
    relative = re.search(prefix+r"(今天|今日|当天|今晚|明天|明日)(?:\s*(\d{1,2})[:：](\d{2}))?", text)
    try:
        if absolute:
            year, month, day, hour, minute = absolute.groups()
            moment = datetime(int(year or base.year), int(month), int(day), int(hour or 23), int(minute or 59), 59, tzinfo=base.tzinfo)
            if year is None and (moment-base).days < -180:
                moment = moment.replace(year=base.year+1)
        elif relative:
            word, hour, minute = relative.groups()
            day = base + timedelta(days=1 if word in {"明天", "明日"} else 0)
            moment = day.replace(hour=int(hour or 23), minute=int(minute or 59), second=59, microsecond=0)
        else:
            return None, "未说明"
        return moment.timestamp(), moment.isoformat(timespec="minutes")
    except ValueError:
        return None, "文字日期无效，待核"


def extract_price_offer(text, now=None, reference_ts=None):
    now = time.time() if now is None else now
    reference_ts = now if reference_ts is None else reference_ts
    if not isinstance(text, str) or len(text) > 100_000:
        return None
    # Text messages alone; no remote OCR, page lookup, or AI service.
    if not PROTEIN.search(text) or not DEAL.search(text):
        return None
    lines = [clean(x) for x in text.splitlines()]
    products = [x for x in lines if x and PROTEIN.search(x)]
    if not products:
        return None
    if re.search(r"摇摇杯|量勺|空桶|蛋白棒", products[0]) and not re.search(r"\d\s*(?:kg|g|磅|克|千克|公斤)", products[0], re.I):
        return None
    # A question/request with no actual quoted price or coupon is not an offer.
    if re.search(r"求推荐|求优惠|蹲好价|哪里买|有没有.*优惠|怎么选", text) and not re.search(r"到手|券后|[￥¥]\s*\d|\d+\s*元", text):
        return None
    brands = []
    if re.search(r"(?<![A-Za-z0-9])ON(?![A-Za-z0-9])|optimum\s+nutrition", text, re.I):
        brands.append("ON")
    if "熊猫" in text:
        brands.append("熊猫")
    if not brands:
        return None
    brand = brands[0] if len(brands)==1 else "品牌待核"
    threshold = {"ON":580, "熊猫":400}.get(brand)
    title = products[0][:120]
    reasons = [] if len(brands)==1 else ["同条消息含多个目标品牌，价格对应关系待核"]
    number = r"(?<![0-9.])[0-9]+(?:\.[0-9]{1,2})?(?![0-9.])"
    final = list(re.finditer(r"(?:到手|实付|券后|折后|入手|仅需|低至|活动价)([价约最低\s:：]*)(￥|¥|人民币|RMB|CNY)?\s*("+number+r")\s*(元|人民币|RMB|CNY)?", text, re.I))
    records = []
    if final:
        for match in final:
            approximate, currency_before, amount, currency_after = match.groups()
            clause = re.split(r"[，,；;\n。]", text[max(0,match.start()-12):match.end()+8])
            foreign = re.search(r"美元|美金|港币|港元|欧元|日元|韩元|\bUSD\b|\bHKD\b|\bJPY\b", " ".join(clause), re.I)
            records.append((float(amount), bool(currency_before or currency_after) and not foreign))
            if "约" in approximate or re.match(r"\s*(?:起|左右|[-~～至到]\s*\d)", text[match.end():]):
                reasons.append("报价是约数、区间或起价")
    else:
        for match in re.finditer(r"(?:(￥|¥|人民币|RMB|CNY)\s*("+number+r")|("+number+r")\s*(元|人民币|RMB|CNY))", text, re.I):
            before = re.split(r"[，,；;\n。]", text[max(0, match.start()-12):match.start()])[-1]
            after = text[match.end():match.end()+5]
            if re.search(r"原价|日常|划线|立减|补贴|返|满|减|券|礼金", before) or re.match(r"\s*(?:券|淘礼金|优惠券|红包|补贴)", after):
                continue
            records.append((float(match.group(2) or match.group(3)), not bool(re.search(r"美元|美金|港币|港元|欧元|日元|韩元|USD|HKD|JPY", before+after, re.I))))
    records = [(p,rmb) for p,rmb in records if 1 <= p <= 20_000]
    prices = sorted({p for p,_ in records})[:12]
    if len(prices) != 1:
        reasons.append("价格缺失或存在多个不同报价")
    if not records or not all(currency for _,currency in records):
        reasons.append("人民币币种未明确")
    if re.search(r"预计|大概|返后|返利后|折合|估算|约\s*\d+\s*元|\d+\s*元?\s*起", text):
        reasons.append("实际到手价仍需确认")
    weights, pounds = [], []
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(公斤|千克|kg|克|g|磅|lbs?)(?![A-Za-z])", text, re.I):
        unit = unit.lower()
        value = float(number)
        weights.append(round(value * (1000 if unit in {"公斤", "千克", "kg"} else 453.59237 if unit in {"磅", "lb", "lbs"} else 1), 2))
        if unit in {"磅", "lb", "lbs"}:
            pounds.append(value)
    weights = sorted({w for w in weights if 50 <= w <= 25_000})
    if pounds and 5 not in pounds:
        return None
    if not pounds and weights and not any(2200 <= w <= 2400 for w in weights):
        return None
    multiple = bool(re.search(r"(?:[×*xX]\s*(?:[2-9]|\d{2,})|[2-9]\s*[桶罐袋盒包件]|[两二三四五六七八九十双]\s*[桶罐袋盒包件]|买\d|送\d)", text))
    if set(pounds) != {5} or len(weights) != 1 or multiple:
        reasons.append("未明确为单件5磅规格或存在多个规格")
    weight = weights[0] if len(weights) == 1 and not multiple else None
    unit_price = round(prices[0] * 1000 / weight, 2) if weight and len(prices) == 1 else None
    conditions = [name for name, pattern in CONDITIONS if re.search(pattern, text, re.I)]
    link = public_link(text)
    expires, expiry_label = expiry(text, reference_ts)
    status = "needs_review" if reasons else "matched"
    if expires is not None and expires <= now:
        status = "expired"
        reasons.append("消息注明的优惠截止时间已过")
    # An unambiguous price that is at or above every possible threshold is irrelevant.
    limit = threshold if threshold is not None else 580
    if prices and min(prices) >= limit and all(currency for _,currency in records):
        return None
    if expiry_label == "文字日期无效，待核":
        status = "needs_review"
        reasons.append(expiry_label)
    normalized = re.sub(r"[\W_]+", "", title.lower())
    signature = repr((normalized, prices, weight, conditions, link, brand, status, expires))
    fingerprint = hashlib.sha256(signature.encode()).hexdigest()
    return Offer(title, prices, weight, unit_price, conditions, link, fingerprint,
                 brand, threshold, status, sorted(set(reasons)), expires, expiry_label)


def keyword_matches(text):
    if not isinstance(text,str):
        return []
    value=unicodedata.normalize("NFKC",text)
    value=re.sub(r"[\u200b-\u200d\ufeff]","",value)
    return list(dict.fromkeys(re.sub(r"\s+","",match.group()) for match in KEYWORD.finditer(value)))


def keyword_conditions(text):
    flags=[name for name,pattern in CONDITIONS if re.search(pattern,text,re.I)]
    if re.search(r"非\s*88\s*vip",text,re.I) and "88VIP" in flags:
        flags.remove("88VIP")
        flags.append("含非88VIP操作说明")
    for name,pattern in (("淘金币",r"淘金币"),("超市卡",r"超市卡"),("退凑单",r"退(?:掉)?\s*凑单")):
        if re.search(pattern,text):
            flags.append(name)
    return flags


def extract_keyword_offer(text,now,reference_ts):
    matched=keyword_matches(text)
    lines=[clean(line) for line in text.splitlines()]
    hits=[i for i,line in enumerate(lines) if keyword_matches(line)]
    if hits:
        position=hits[0]
        excerpt=lines[position][:240]
        product_lines=[line for line in lines[max(0,position-3):position+1] if PROTEIN.search(line)]
        title=(product_lines[-1] if product_lines else lines[position])[:120]
    else:
        # A keyword on a coupon-password line is still a mention, but do not
        # retain the password or contact text removed by clean().
        title="群消息提到超好价"
        excerpt="命中关键词："+"、".join(matched)
    expires,label=expiry(text,reference_ts)
    if expires is not None and expires<=now:
        label+="（文字注明的截止时间已过）"
    conditions=keyword_conditions(text)
    link=public_link(text)
    canonical=lambda value: re.sub(r"[\W_]+","",KEYWORD.sub("超好价",unicodedata.normalize("NFKC",value)).lower())
    signature=repr(("keyword:超好价",canonical(title),canonical(excerpt),conditions,link,label))
    fingerprint=hashlib.sha256(signature.encode()).hexdigest()
    # A keyword mention is not a claim that the offer meets either price rule.
    # Even uncertain/expired quoted deals trigger the explicit mention rule,
    # with validity described in the notification instead of silently filtered.
    return Offer(title,[],None,None,conditions,link,fingerprint,"关键词提醒",None,
                 "matched",[],None,label,"keyword",matched,excerpt)


def extract(text,now=None,reference_ts=None):
    now=time.time() if now is None else now
    reference_ts=now if reference_ts is None else reference_ts
    if not isinstance(text,str) or len(text)>100_000:
        return None
    if keyword_matches(text):
        return extract_keyword_offer(text,now,reference_ts)
    return extract_price_offer(text,now=now,reference_ts=reference_ts)
