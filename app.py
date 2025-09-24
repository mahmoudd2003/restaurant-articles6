# ضمان مجلد التخزين
import os
os.makedirs("data", exist_ok=True)

# الاستيراد (حسب مكان الملف عندك)
try:
    from category_criteria import get_category_criteria
except ImportError:
    from modules.category_criteria import get_category_criteria  # لو نقلته داخل utils/modules

import io, csv, unicodedata, json
from datetime import datetime
from pathlib import Path

import streamlit as st

from utils.openai_client import get_client, chat_complete
from utils.exporters import to_docx, to_json
from utils.content_fetch import fetch_and_extract
from utils.competitor_analysis import analyze_competitors, extract_gap_points
from utils.quality_checks import quality_report
from utils.llm_reviewer import llm_review, llm_fix

# إضافات ووردبريس
import requests
import markdown as md  # لتحويل Markdown إلى HTML قبل النشر

# ========== Helpers for Places Integration (normalize + protected details) ==========
import re, unicodedata as _ud
from difflib import SequenceMatcher
from urllib.parse import urlparse  # لعرض الدومين في الشريط الجانبي

_AR_DIAC = re.compile(r'[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]')
_PUNCT  = re.compile(r'[^\w\s\u0600-\u06FF]')

def normalize_ar(s: str) -> str:
    if not s: return ""
    s = _ud.normalize("NFKC", s)
    s = _AR_DIAC.sub("", s)
    s = s.replace("أ","ا").replace("إ","ا").replace("آ","ا").replace("ى","ي")
    s = s.replace("ؤ","و").replace("ئ","ي").replace("ة","ه").replace("ـ","")
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    s = s.translate(trans)
    return s

def best_match(name: str, index: dict, threshold: float = 0.90):
    key = normalize_ar(name)
    if key in index:
        return index[key]
    best_key, best_score = None, 0.0
    for k in index.keys():
        sc = SequenceMatcher(None, key, k).ratio()
        if sc > best_score:
            best_key, best_score = k, sc
    return index.get(best_key) if best_score >= threshold else None

def _fmt(v):
    return str(v).strip() if v and str(v).strip() else "غير متوفر"

def _link(label, url):
    return f"[{label}]({url})" if url and str(url).strip() else "غير متوفر"

def render_details_block(item: dict) -> str:
    address = _fmt(item.get("address"))
    phone   = _fmt(item.get("phone"))
    hours   = _fmt(item.get("thursday_hours"))
    family  = _fmt(item.get("family_friendly"))  # "نعم (تقديري)" / "لا (تقديري)" / غير متوفر
    pricepp = _fmt(item.get("price_per_person"))
    dish    = _fmt(item.get("signature_dish"))   # "—" أو اسم طبق
    busy    = _fmt(item.get("busy_times"))
    mapslnk = _link("فتح في خرائط Google", item.get("maps_url"))
    webslnk = _link("زيارة الموقع", item.get("website"))
    return (
        "\n**تفاصيل عملية:**\n"
        f"- **العنوان:** {address}\n"
        f"- **الهاتف:** {phone}\n"
        f"- **الأوقات:** {hours}\n"
        f"- **مناسب للعوائل:** {family}\n"
        f"- **السعر للشخص:** {pricepp}\n"
        f"- **الطبق المميز:** {dish}\n"
        f"- **أوقات الزحمة:** {busy}\n"
        f"- **خرائط Google:** {mapslnk}\n"
        f"- **الموقع الإلكتروني:** {webslnk}\n"
    )

def inject_details_under_h3(markdown_text: str, places_index: dict) -> str:
    """
    بعد كل '### <اسم المطعم>' والفقرة الأولى التي تليه، أدرج كتلة 'تفاصيل عملية'
    بمطابقة الاسم مع places_index (محمية 100%). إن لم نجد المطابقة، نعرض 'غير متوفر'.
    """
    if not markdown_text or not places_index:
        return markdown_text

    lines = markdown_text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)

        if line.startswith("### "):
            h3_name = line[4:].strip()
            # احتفظ بأي أسطر فارغة بعد H3
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                out.append(lines[j]); j += 1
            # الفقرة الأولى (حتى سطر فارغ أو عنوان جديد)
            while j < len(lines) and not lines[j].startswith("#") and lines[j].strip() != "":
                out.append(lines[j]); j += 1

            matched = best_match(h3_name, places_index, threshold=0.90)
            if matched is None:
                matched = {
                    "address": None, "phone": None, "thursday_hours": None,
                    "family_friendly": None, "price_per_person": None,
                    "signature_dish": "—", "busy_times": None,
                    "maps_url": None, "website": None
                }
            out.append(render_details_block(matched))
            i = j
            continue

        i += 1

    return "\n".join(out)
# ========== End Helpers ======================================================

# ========== (جديد) Helpers لفرض الأقسام واللمسات والطول ==========
def _word_count(md: str) -> int:
    """عدّ كلمات عربي/لاتيني بعد إزالة ماركداون بسيطة."""
    txt = re.sub(r"[#>*_`~\[\]\(\)]", " ", md or "")
    tokens = re.findall(r"[\u0600-\u06FF\w]+", txt, flags=re.UNICODE)
    return len(tokens)

FAQ_PATTERNS = [
    r"^##\s*FAQ\s*$",
    r"^##\s*الأسئلة\s+الشائعة\s*$",
    r"^##\s*أسئلة\s+شائعة\s*$",
]
METH_PATTERNS = [
    r"^##\s*منهجية\s*التحرير\s*$",
    r"^##\s*منهجية\s*$",
    r"^##\s*طريقة\s*العمل\s*$",
]

def _has_section(md: str, patterns) -> bool:
    if not md: return False
    for p in patterns:
        if re.search(p, md, flags=re.IGNORECASE | re.MULTILINE | re.UNICODE):
            return True
    return False

PROMPTS_DIR = Path("prompts")
def read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")

def _read_template_file(name: str) -> str:
    """يقرأ من prompts/<name>، وإن لم يوجد جرّب المسار الجذري."""
    try:
        return read_prompt(name)
    except Exception:
        p = Path(name)
        return p.read_text(encoding="utf-8") if p.exists() else ""

def ensure_sections(article_md: str, need_faq: bool, need_meth: bool) -> str:
    """يدرج FAQ/منهجية إن غابا، من القوالب faq.md/methodology.md."""
    out = article_md or ""
    if need_faq and not _has_section(out, FAQ_PATTERNS):
        faq_block = _read_template_file("faq.md").strip()
        if faq_block:
            out += ("\n\n" + faq_block + "\n")
    if need_meth and not _has_section(out, METH_PATTERNS):
        meth_block = _read_template_file("methodology.md").strip()
        if meth_block:
            out += ("\n\n" + meth_block + "\n")
    return out

def add_human_fallback(article_md: str) -> str:
    """Fallback بسيط لو لم تنجح طبقة Polish."""
    if re.search(r"^##\s*لمسات\s+بشرية\s*$", article_md or "", flags=re.MULTILINE):
        return article_md
    extra = (
        "\n\n## لمسات بشرية\n"
        "• تجارب مباشرة قابلة للتحقق: زيارات فعلية وتذوّق أطباق محددة.\n"
        "• مصادر موثوقة: مراجعات موثقة وبيانات رسمية من المواقع.\n"
        "• تفاصيل عملية: سعر تقريبي، أفضل وقت للزيارة، نصيحة للذروة.\n"
        "• شفافية: التنبيه لإمكانية تغيّر القوائم/الأسعار.\n"
    )
    return (article_md or "") + extra

def enforce_word_target_via_llm(client, primary_model, fallback_model, article_md: str,
                                target_words: int, tolerance_pct: int = 12) -> str:
    """يوسّع/يختصر للوصول إلى الهدف مع الحفاظ على العناوين وFAQ/المنهجية."""
    wc = _word_count(article_md)
    low = int(target_words * (1 - tolerance_pct/100))
    high = int(target_words * (1 + tolerance_pct/100))
    if low <= wc <= high:
        return article_md

    if wc < low:
        delta = low - wc
        instr = (
            f"وسّع المحتوى بنحو ~{delta} كلمة ليصبح قريبًا من {target_words} كلمة، "
            "مع الحفاظ على البنية والعناوين كما هي. أضف تفاصيل عملية موثوقة "
            "وتجنّب الحشو والمجاز. لا تحذف قسمي FAQ ومنهجية التحرير إن وُجدا."
        )
    else:
        delta = wc - high
        instr = (
            f"اختصر المحتوى بنحو ~{delta} كلمة ليصبح قريبًا من {target_words} كلمة، "
            "مع إزالة التكرار والحشو مع الحفاظ على البنية والعناوين. لا تلمس FAQ ومنهجية التحرير."
        )

    messages = [
        {"role": "system", "content": "أنت محرر عربي يوازن بين الإيجاز والإشباع ويحافظ على البنية."},
        {"role": "user", "content": f"النص الحالي (لا تغيّر العناوين):\n\n{article_md}\n\nالتعليمات:\n{instr}"},
    ]
    try:
        refined = chat_complete(client, messages, max_tokens=2400, temperature=0.3,
                                model=primary_model, fallback_model=fallback_model)
        return refined or article_md
    except Exception:
        return article_md
# ========== End enforce helpers =============================================

st.set_page_config(page_title="مولد مقالات المطاعم (E-E-A-T)", page_icon="🍽️", layout="wide")
st.title("🍽️ مولد مقالات المطاعم — E-E-A-T + Human Touch + منافسين + فحص بشرية")

# (نُبقي على تحميل القوالب كما هي لديك)
BASE_TMPL = read_prompt("base.md")
POLISH_TMPL = read_prompt("polish.md")
FAQ_TMPL = read_prompt("faq.md")
METH_TMPL = read_prompt("methodology.md")
CRITERIA_MAP = {
    "بيتزا": read_prompt("criteria_pizza.md"),
    "مندي": read_prompt("criteria_mandy.md"),
    "برجر": read_prompt("criteria_burger.md"),
    "كافيهات": read_prompt("criteria_cafes.md"),
}
GENERAL_CRITERIA = read_prompt("criteria_general.md")

def _has_api_key() -> bool:
    try:
        if hasattr(st, "secrets") and "OPENAI_API_KEY" in st.secrets and st.secrets["OPENAI_API_KEY"]:
            return True
    except Exception:
        pass
    return bool(os.getenv("OPENAI_API_KEY"))

def slugify(name: str) -> str:
    s = ''.join(c for c in unicodedata.normalize('NFKD', name) if not unicodedata.combining(c))
    import re as _re
    s = _re.sub(r'\W+', '_', s).strip('_').lower()
    return s or "custom"

PLACE_TEMPLATES = {
    "مول/مجمع": "احجز قبل الذروة بـ20–30 دقيقة، راقب أوقات العروض/النافورة، وتجنّب طوابير المصاعد.",
    "جهة من المدينة (شمال/شرق..)": "الوصول أسهل عبر الطرق الدائرية قبل 7:30م، مواقف الشوارع قد تمتلئ مبكرًا في الويكند.",
    "حيّ محدد": "المشي بعد العشاء خيار لطيف إن توفّرت أرصفة هادئة، انتبه لاختلاف الذروة بين أيام الأسبوع والويكند.",
    "شارع/ممشى": "الجلسات الخارجية ألطف بعد المغرب صيفًا، والبرد الليلي قد يتطلّب مشروبًا ساخنًا شتاءً.",
    "واجهة بحرية/كورنيش": "الهواء أقوى مساءً—اطلب المشروبات سريعًا ويُفضّل المقاعد البعيدة عن التيارات.",
    "فندق/منتجع": "قد ترتفع الأسعار لكن الخدمة أدقّ، احجز باكرًا لأماكن النوافذ/الإطلالات.",
    "مدينة كاملة": "فروع سلسلة واحدة قد تختلف جودتها بين الأحياء، اطلب الطبق الأشهر أولًا لتقييم المستوى."
}
def build_protip_hint(place_type: str) -> str:
    return PLACE_TEMPLATES.get(place_type or "", "قدّم نصيحة عملية مرتبطة بالمكان والذروة وسهولة الوصول.")
def build_place_context(place_type: str, place_name: str, place_rules: str, strict: bool) -> str:
    scope = "صارم (التزم بالنطاق فقط)" if strict else "مرن (الأولوية داخل النطاق)"
    return f"""سياق المكان:
- النوع: {place_type or "غير محدد"}
- الاسم: {place_name or "غير محدد"}
- قواعد النطاق: {place_rules or "—"}
- صرامة الالتزام بالنطاق: {scope}"""

# Sidebar
st.sidebar.header("⚙️ الإعدادات العامة")
tone = st.sidebar.selectbox(
    "نغمة الأسلوب",
    ["ناقد ودود", "ناقد صارم", "دليل تحريري محايد", "ناقد صارم | مراجعات الجمهور", "ناقد صارم | تجربة مباشرة + مراجعات"]
)
primary_model = st.sidebar.selectbox("اختر الموديل الأساسي", ["gpt-4.1", "gpt-4o", "gpt-4o-mini"], index=0)
fallback_model = st.sidebar.selectbox("موديل بديل (Fallback)", ["gpt-4o", "gpt-4o-mini", "gpt-4.1"], index=1)
include_faq = st.sidebar.checkbox("إضافة قسم FAQ", value=True)
include_methodology = st.sidebar.checkbox("إضافة منهجية التحرير", value=True)
add_human_touch = st.sidebar.checkbox("تفعيل طبقة اللمسات البشرية (Polish)", value=True)
approx_len = st.sidebar.slider("الطول التقريبي (كلمات)", 600, 1800, 1100, step=100)
# (الهامش 12% ثابت — يمكن ترقية الواجهة لاحقًا إن أردت)

review_weight = None
if tone in ["ناقد صارم | مراجعات الجمهور", "ناقد صارم | تجربة مباشرة + مراجعات"]:
    default_weight = 85 if tone == "ناقد صارم | مراجعات الجمهور" else 55
    review_weight = st.sidebar.slider("وزن الاعتماد على المراجعات (٪)", 0, 100, default_weight, step=5)

st.sidebar.markdown("---")
st.sidebar.subheader("🔗 روابط داخلية (اختياري)")
internal_catalog = st.sidebar.text_area(
    "أدخل عناوين/سلاگز مقالاتك (سطر لكل عنصر)",
    "أفضل مطاعم الرياض\nأفضل مطاعم إفطار في الرياض\nأفضل مطاعم بيتزا في جدة"
)

# ====== إعدادات ووردبريس في الشريط الجانبي ======
st.sidebar.markdown("---")
st.sidebar.subheader("🗝️ إعدادات ووردبريس (للتحقق السريع)")

def _get_secret_fuzzy(primary: str, aliases: tuple[str, ...] = ()):
    names = (primary,) + aliases
    for nm in names:
        v = None
        try:
            if hasattr(st, "secrets"):
                v = st.secrets.get(nm)
        except Exception:
            v = None
        if not v:
            v = os.getenv(nm)
        if isinstance(v, str):
            v = v.strip()
        if v:
            return v
    try:
        all_keys = list(getattr(st, "secrets", {}).keys())
    except Exception:
        all_keys = []
    norm = lambda s: "".join(s.split()).lower() if isinstance(s, str) else ""
    target_set = {norm(nm) for nm in names}
    for k in all_keys:
        if norm(k) in target_set:
            v = st.secrets.get(k)
            if isinstance(v, str):
                v = v.strip()
            if v:
                return v
    return None

def _mask_value(val: str, show_last: int = 4) -> str:
    if not val:
        return "—"
    s = str(val)
    s_clean = "".join(s.split())
    if len(s_clean) <= show_last:
        return "•" * len(s_clean)
    return "•" * (len(s_clean) - show_last) + s_clean[-show_last:]

def _domain_from_url(u: str) -> str:
    if not u: return "—"
    try:
        netloc = urlparse(u).netloc
        return netloc or u
    except Exception:
        return u

st.sidebar.caption(
    f"WP_BASE_URL: {'OK' if _get_secret_fuzzy('WP_BASE_URL', ('WP_URL','WORDPRESS_BASE_URL')) else 'MISSING'} · "
    f"WP_USER: {'OK' if _get_secret_fuzzy('WP_USER', ('WORDPRESS_USER','WP_USERNAME')) else 'MISSING'} · "
    f"WP_APP_PASS: {'OK' if _get_secret_fuzzy('WP_APP_PASS', ('WP_APP_PASSWORD','WORDPRESS_APP_PASSWORD')) else 'MISSING'}"
)

reveal_wp = st.sidebar.checkbox("إظهار اسم المستخدم والدومين (لن نعرض كلمة المرور)", value=False)
wp_base_sb = _get_secret_fuzzy("WP_BASE_URL", aliases=("WP_URL", "WORDPRESS_BASE_URL"))
wp_user_sb = _get_secret_fuzzy("WP_USER", aliases=("WORDPRESS_USER", "WP_USERNAME"))
wp_pass_sb = _get_secret_fuzzy("WP_APP_PASS", aliases=("WP_APP_PASSWORD", "WORDPRESS_APP_PASSWORD"))
st.sidebar.write("**المضيف (الدومين):** ", _domain_from_url(wp_base_sb) if reveal_wp else "مخفي")
st.sidebar.write("**اسم المستخدم:** ", (wp_user_sb or "—") if reveal_wp else _mask_value(wp_user_sb or "", show_last=0))
st.sidebar.write("**كلمة مرور التطبيق:** ", _mask_value(wp_pass_sb or ""))  # دائمًا مُقنّعة
if reveal_wp and wp_base_sb:
    st.sidebar.markdown(f"[اختبار REST]({wp_base_sb.rstrip('/')}/wp-json/)")

# Tabs (أضفنا تبويب Google كـ رابع تبويب)
tab_article, tab_comp, tab_qc, tab_places = st.tabs([
    "✍️ توليد المقال",
    "🆚 تحليل المنافسين (روابط يدوية)",
    "🧪 فحص بشرية وجودة المحتوى",
    "🌍 جلب مطاعم من Google"
])

# ------------------ Tab 1: Article Generation ------------------
with tab_article:
    col1, col2 = st.columns([2,1])
    with col1:
        article_title = st.text_input("عنوان المقال", "أفضل مطاعم في الرياض")
        keyword = st.text_input("الكلمة المفتاحية (اختياري)", "مطاعم في الرياض")

        COUNTRIES = {"السعودية": ["الرياض","جدة","مكة","المدينة المنورة","الدمام","الخبر","الظهران","الطائف","أبها","خميس مشيط","جازان","نجران","تبوك","بريدة","عنيزة","الهفوف","الأحساء","الجبيل","القطيف","ينبع","حائل"],
                     "الإمارات": ["دبي","أبوظبي","الشارقة","عجمان","رأس الخيمة","الفجيرة","أم القيوين","العين"]}
        country = st.selectbox("الدولة", ["السعودية", "الإمارات", "أخرى…"], index=0)
        if country in COUNTRIES:
            city_choice = st.selectbox("المدينة", COUNTRIES[country] + ["مدينة مخصّصة…"], index=0)
            city_input = st.text_input("أدخل اسم المدينة", city_choice) if city_choice == "مدينة مخصّصة…" else city_choice
        else:
            country = st.text_input("اسم الدولة", "السعودية")
            city_input = st.text_input("المدينة", "الرياض")

        place_type = st.selectbox("نوع المكان",
            ["مول/مجمع", "جهة من المدينة (شمال/شرق..)", "حيّ محدد", "شارع/ممشى", "واجهة بحرية/كورنيش", "فندق/منتجع", "مدينة كاملة"], index=0)
        place_name = st.text_input("اسم المكان/النطاق", placeholder="مثلًا: دبي مول / شمال الرياض")
        place_rules = st.text_area("قواعد النطاق (اختياري)", placeholder="داخل المول فقط، أو الأحياء: الربيع/الياسمين/المروج…", height=80)
        strict_in_scope = st.checkbox("التزم بالنطاق الجغرافي فقط (صارم)", value=True)

        content_scope = st.radio("نطاق المحتوى", ["فئة محددة داخل المكان", "شامل بلا فئة", "هجين (تقسيم داخلي)"], index=1 if place_type=="مول/مجمع" else 0)

        built_in_labels = list(CRITERIA_MAP.keys())
        category = "عام"
        criteria_block = GENERAL_CRITERIA

        # ---------------- تحديد الفئة وبناء النص الأولي + علامة هل هي مخصّصة ----------------
        is_custom_category = False
        if content_scope == "فئة محددة داخل المكان":
            category_choice = st.selectbox("الفئة", built_in_labels + ["فئة مخصّصة…"])

            if category_choice == "فئة مخصّصة…":
                # حقن القيمة المعلّقة (إن وُجدت) قبل إنشاء Text Area
                if "pending_custom_criteria_text" in st.session_state:
                    st.session_state["custom_criteria_text"] = st.session_state.pop("pending_custom_criteria_text")

                custom_category_name = st.text_input("اسم الفئة المخصّصة", "مطاعم لبنانية", key="custom_category_name")

                # لا نمرّر value إذا كان المفتاح موجودًا؛ فقط أول تشغيل
                DEFAULT_CRIT_MD = (
                    "- **التجربة المباشرة:** زيارات متعدّدة وتجربة أطباق أساسية ومعروفة في المطبخ.\n"
                    "- **المكوّنات:** جودة اللحوم/الأسماك/الأجبان والخضروات الطازجة.\n"
                    "- **طرق الطهي والأصالة:** التتبيل والتحمير/الشوي/الفرن ومدى اقتراب النكهة من الأصل.\n"
                    "- **الأجواء والملاءمة:** جلسات عائلية/أصدقاء، مستوى الضجيج وراحة الجلسات.\n"
                    "- **ثبات الجودة:** ملاحظة التماسك في الطعم والخدمة عبر زيارات وأوقات مختلفة."
                )
                ta_kwargs = dict(key="custom_criteria_text", height=140)
                if "custom_criteria_text" not in st.session_state:
                    ta_kwargs["value"] = DEFAULT_CRIT_MD

                custom_criteria_text = st.text_area(
                    "معايير الاختيار لهذه الفئة (يدوي — اختياري، سيتم استبدالها تلقائيًا عند الضغط على زر الجلب)",
                    **ta_kwargs
                )

                category = (st.session_state.get("custom_category_name") or "فئة مخصّصة").strip()
                criteria_block = st.session_state.get("custom_criteria_text") or "اعتمدنا على التجربة المباشرة، جودة المكونات، تنوع القائمة، وثبات الجودة."
                is_custom_category = True
            else:
                category = category_choice
                criteria_block = CRITERIA_MAP.get(category_choice, GENERAL_CRITERIA)
                is_custom_category = False
        else:
            category = "عام"
            criteria_block = GENERAL_CRITERIA
            is_custom_category = False
        # ---------------------------------------------------------------------

        # ---------- دوال تطبيع العرض + زر/خيار جلب/توليد معايير الفئة ----------
        def _normalize_criteria(raw):
            """حوّل أي ناتج (list/tuple/dict/str JSON) إلى قائمة نصوص نظيفة بلا undefined."""
            if raw is None:
                return []
            if isinstance(raw, str):
                s = raw.strip()
                if s.startswith(("[", "{")):
                    try:
                        raw = json.loads(s)
                    except Exception:
                        lines = [ln.strip(" -•\t").strip() for ln in s.splitlines() if ln.strip()]
                        return [ln for ln in lines if ln and ln.lower() != "undefined"]
                else:
                    lines = [ln.strip(" -•\t").strip() for ln in s.splitlines() if ln.strip()]
                    return [ln for ln in lines if ln and ln.lower() != "undefined"]
            if isinstance(raw, dict):
                for k in ("criteria", "bullets", "items", "list"):
                    if k in raw:
                        raw = raw[k]
                        break
                else:
                    vals = list(raw.values())
                    raw = vals if all(isinstance(v, str) for v in vals) else list(raw.keys())
            if isinstance(raw, (list, tuple)):
                out = []
                for x in raw:
                    if isinstance(x, str):
                        t = x.strip().strip(",").strip('"').strip("'")
                    elif isinstance(x, dict) and "text" in x:
                        t = str(x["text"]).strip()
                    else:
                        t = str(x).strip()
                    if t and t.lower() != "undefined":
                        out.append(t)
                return out
            return [str(raw)]

        def _format_criteria_md(items):
            items = _normalize_criteria(items)
            return "\n".join(f"- {c}" for c in items) or "- —"

        effective_category = (category or "عام").strip()
        if "criteria_generated_md_map" not in st.session_state:
            st.session_state["criteria_generated_md_map"] = {}

        with st.expander("📋 معايير الاختيار لهذه الفئة (تلقائي/يدوي)", expanded=False):
            st.caption(f"الفئة الحالية: **{effective_category}**")
            use_llm = st.checkbox("تعزيز بالـ LLM (اختياري)", value=False, key="crit_llm",
                                  help="يتطلب OPENAI_API_KEY إن فعّلته، وإلا تُستخدم Heuristics.")
            if st.button("جلب/توليد معايير الفئة", key="btn_generate_criteria"):
                crit_list = get_category_criteria(
                    effective_category,
                    use_llm=use_llm,
                    catalog_path="data/criteria_catalog.yaml"
                )
                md_ = _format_criteria_md(crit_list)
                st.session_state["criteria_generated_md_map"].pop(effective_category, None)
                st.session_state["criteria_generated_md_map"][effective_category] = md_

                if is_custom_category:
                    st.session_state["pending_custom_criteria_text"] = md_
                    if getattr(st, "rerun", None):
                        st.rerun()
                    else:
                        st.experimental_rerun()
                else:
                    st.success("تم توليد المعايير وحفظها.")

            if effective_category in st.session_state["criteria_generated_md_map"]:
                st.markdown("**المعايير (تلقائي):**")
                st.markdown(st.session_state["criteria_generated_md_map"][effective_category])

        if is_custom_category:
            criteria_block = st.session_state.get("custom_criteria_text", criteria_block)
        else:
            criteria_block = st.session_state.get("criteria_generated_md_map", {}).get(effective_category, criteria_block)

        restaurants_input = st.text_area(
            "أدخل أسماء المطاعم (سطر لكل مطعم)",
            st.session_state.get("restaurants_text", "مطعم 1\nمطعم 2\nمطعم 3"),
            height=160
        )
        st.markdown("**أو** ارفع ملف CSV بأسماء المطاعم (عمود: name)")
        csv_file = st.file_uploader("رفع CSV (اختياري)", type=["csv"], help="عمود name مطلوب؛ عمود note اختياري.")

        def _normalize_name(s: str) -> str:
            return " ".join((s or "").strip().split())
        def _merge_unique(a: list, b: list) -> list:
            seen, out = set(), []
            for x in a + b:
                x2 = _normalize_name(x)
                if x2 and x2 not in seen:
                    seen.add(x2); out.append(x2)
            return out

        typed_restaurants = [r.strip() for r in restaurants_input.splitlines() if r.strip()]
        uploaded_restaurants = []
        if csv_file:
            try:
                text = csv_file.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(text))
                for row in reader:
                    name = row.get("name") or row.get("اسم") or ""
                    if name.strip():
                        uploaded_restaurants.append(name.strip())
            except Exception as e:
                st.warning(f"تعذّر قراءة CSV: {e}")
        restaurants = _merge_unique(typed_restaurants, uploaded_restaurants)

        manual_notes = st.text_area("ملاحظات يدوية تُدمج داخل التجارب (اختياري)", st.session_state.get("comp_gap_notes",""))

    with col2:
        st.subheader("قائمة تدقيق بشرية")
        checks = {
            "sensory": st.checkbox("أضف وصفًا حسيًا دقيقًا (رائحة/قوام/حرارة) لمطعم واحد على الأقل"),
            "personal": st.checkbox("أدرج ملاحظة شخصية/تفضيل شخصي"),
            "compare": st.checkbox("أضف مقارنة صغيرة مع زيارة سابقة/مطعم مشابه"),
            "critique": st.checkbox("أضف نقدًا غير متوقع (تفصيلة سلبية صغيرة)"),
            "vary": st.checkbox("نوّع أطوال الفقرات لتجنب الرتابة"),
        }

    if st.button("🚀 توليد المقال"):
        if not _has_api_key():
            st.error("لا يوجد OPENAI_API_KEY.")
            st.stop()
        client = get_client()

        if tone == "ناقد صارم | مراجعات الجمهور":
            tone_instructions = ("اكتب كنّاقد صارم يعتمد أساسًا على مراجعات العملاء المنشورة علنًا. "
                                 "ركّز على الأنماط المتكررة واذكر حدود المنهجية. لا تدّعِ visita شخصية. لا تستخدم أرقام.")
            tone_selection_line = "اعتمدنا على مراجعات موثوقة منشورة علنًا حتى {last_updated}، مع التركيز على الأنماط المتكررة."
            system_tone = "أسلوب ناقد صارم مرتكز على مراجعات الجمهور"
        elif tone == "ناقد صارم | تجربة مباشرة + مراجعات":
            tone_instructions = ("اكتب كنّاقد صارم يمزج خبرة ميدانية مع مراجعات الجمهور. "
                                 "قدّم الحكم من التجربة المباشرة أولًا ثم قارنه بانطباعات الجمهور. أدرج **نقطة للتحسين** لكل مطعم.")
            tone_selection_line = "مزجنا بين زيارات ميدانية وتجارب فعلية ومراجعات عامة حتى {last_updated}."
            system_tone = "أسلوب ناقد صارم يمزج التجربة المباشرة مع مراجعات الجمهور"
        else:
            tone_instructions = "اكتب بأسلوب متوازن يراعي الدقة والوضوح دون مبالغة."
            tone_selection_line = "اعتمدنا على التجربة المباشرة ومعلومات موثوقة متاحة، مع مراجعة دورية."
            system_tone = tone

        if content_scope == "فئة محددة داخل المكان":
            scope_instructions = "التزم بالفئة المحددة فقط داخل النطاق الجغرافي."
        elif content_scope == "هجين (تقسيم داخلي)":
            scope_instructions = "قسّم المطاعم إلى أقسام منطقية ووازن التنوع."
        else:
            scope_instructions = "قدّم تشكيلة متنوعة تمثّل المكان."

        protip_hint = build_protip_hint(place_type)
        place_context = build_place_context(place_type, place_name, place_rules, strict_in_scope)

        faq_block = FAQ_TMPL.format(category=category, city=place_name or city_input) if include_faq else "—"
        last_updated = datetime.now().strftime("%B %Y")
        methodology_block = METH_TMPL.format(last_updated=last_updated) if include_methodology else "—"

        base_prompt = BASE_TMPL.format(
            title=article_title, keyword=keyword, content_scope=content_scope, category=category,
            restaurants_list=", ".join(restaurants), criteria_block=criteria_block, faq_block=faq_block,
            methodology_block=methodology_block, tone_label=tone, place_context=place_context,
            protip_hint=protip_hint, scope_instructions=scope_instructions, tone_instructions=tone_instructions,
            tone_selection_line=tone_selection_line.replace("{last_updated}", last_updated)
        )
        base_messages = [
            {"role": "system", "content": f"اكتب المقال بالعربية الفصحى. {system_tone}. طول تقريبي {approx_len} كلمة."},
            {"role": "user", "content": base_prompt},
        ]
        try:
            article_md = chat_complete(client, base_messages, max_tokens=2200, temperature=0.7, model=primary_model, fallback_model=fallback_model)
        except Exception as e:
            st.error(f"فشل التوليد: {e}")
            st.stop()

        # --- طبقة Polish (كما في نسختك) ---
        apply_polish = add_human_touch or any(checks.values())
        merged_user_notes = (st.session_state.get("comp_gap_notes","") + "\n" + (manual_notes or "")).strip()
        if apply_polish or merged_user_notes:
            polish_prompt = read_prompt("polish.md").format(article=article_md, user_notes=merged_user_notes)
            polish_messages = [
                {"role": "system", "content": "أنت محرر عربي محترف، تحافظ على الحقائق وتضيف لمسات بشرية بدون مبالغة."},
                {"role": "user", "content": polish_prompt},
            ]
            try:
                article_md = chat_complete(client, polish_messages, max_tokens=2400, temperature=0.8, model=primary_model, fallback_model=fallback_model)
            except Exception as e:
                st.warning(f"طبقة اللمسات البشرية تعذّرت: {e}")

        # --- ✅ فرض الأقسام واللمسات والطول بعد التوليد ---
        # 1) ضمان وجود FAQ ومنهجية التحرير إن تم تفعيلهما
        article_md = ensure_sections(article_md, need_faq=include_faq, need_meth=include_methodology)

        # 2) fallback لمسات بشرية إن اخترتها وفشل الـ LLM في تحسين واضح
        if add_human_touch:
            article_md = add_human_fallback(article_md)

        # 3) ضبط الطول التقريبي ±12%
        article_md = enforce_word_target_via_llm(
            client, primary_model, fallback_model, article_md, target_words=int(approx_len), tolerance_pct=12
        )

        # 🔁 حقن البطاقات المحمية 100% تحت كل H3 قبل الMeta/Links
        if "places_index" in st.session_state and st.session_state["places_index"]:
            article_md = inject_details_under_h3(article_md, st.session_state["places_index"])

        meta_prompt = f"صِغ عنوان SEO (≤ 60) ووصف ميتا (≤ 155) بالعربية لمقال بعنوان \"{article_title}\". الكلمة المفتاحية: {keyword}.\nTITLE: ...\nDESCRIPTION: ..."
        try:
            meta_out = chat_complete(client, [{"role":"system","content":"أنت مختص SEO عربي."},{"role":"user","content": meta_prompt}], max_tokens=200, temperature=0.6, model=primary_model, fallback_model=fallback_model)
        except Exception:
            meta_out = f"TITLE: {article_title}\nDESCRIPTION: دليل عملي عن {keyword}."

        links_catalog = [s.strip() for s in internal_catalog.splitlines() if s.strip()]
        links_prompt = f"اقترح 3 روابط داخلية مناسبة من هذه القائمة إن أمكن:\n{links_catalog}\nالعنوان: {article_title}\nالنطاق: {content_scope}\nالفئة: {category}\nالمدينة/المكان: {place_name or city_input}\nمقتطف:\n{article_md[:800]}\n- رابط داخلي مقترح: <النص>\n- رابط داخلي مقترح: <النص>\n- رابط داخلي مقترح: <النص>"
        try:
            links_out = chat_complete(client, [{"role":"system","content":"أنت محرر عربي يقترح روابط داخلية طبيعية."},{"role":"user","content": links_prompt}], max_tokens=240, temperature=0.5, model=primary_model, fallback_model=fallback_model)
        except Exception:
            links_out = "- رابط داخلي مقترح: أفضل مطاعم الرياض\n- رابط داخلي مقترح: دليل مطاعم العائلات في الرياض\n- رابط داخلي مقترح: مقارنة بين الأنماط"

        st.subheader("📄 المقال الناتج")
        st.markdown(article_md)
        st.session_state['last_article_md'] = article_md
        st.session_state['last_title'] = article_title  # لحساب slug للنشر

        st.subheader("🔎 Meta (SEO)"); st.code(meta_out, language="text")
        st.subheader("🔗 روابط داخلية مقترحة"); st.markdown(links_out)

        json_obj = {"title": article_title, "keyword": keyword, "category": category,
            "country": country, "city": city_input, "place": {"type": place_type, "name": place_name, "rules": place_rules, "strict": strict_in_scope},
            "content_scope": content_scope, "restaurants": restaurants, "last_updated": datetime.now().strftime("%B %Y"),
            "tone": tone, "reviews_weight": review_weight, "models": {"primary": primary_model, "fallback": fallback_model},
            "include_faq": include_faq, "include_methodology": include_methodology,
            "article_markdown": article_md, "meta": meta_out, "internal_links": links_out}
        st.session_state['last_json'] = to_json(json_obj)

    with col2:
        colA, colB, colC = st.columns(3)
        with colA:
            md_data = st.session_state.get('last_article_md', '')
            st.download_button('💾 تنزيل Markdown', data=md_data, file_name='article.md', mime='text/markdown')
        with colB:
            md_data = st.session_state.get('last_article_md', '')
            st.download_button('📝 تنزيل DOCX', data=to_docx(md_data), file_name='article.docx', mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        with colC:
            json_data = st.session_state.get('last_json', '{}')
            st.download_button('🧩 تنزيل JSON', data=json_data, file_name='article.json', mime='application/json')

    # ==== النشر على ووردبريس (مع تشخيص المفاتيح) ====
    st.markdown("---")
    st.subheader("📰 النشر على ووردبريس")

    wp_base = _get_secret_fuzzy("WP_BASE_URL", aliases=("WP_URL", "WORDPRESS_BASE_URL"))
    wp_user = _get_secret_fuzzy("WP_USER", aliases=("WORDPRESS_USER", "WP_USERNAME"))
    wp_pass = _get_secret_fuzzy("WP_APP_PASS", aliases=("WP_APP_PASSWORD", "WORDPRESS_APP_PASSWORD"))
    wp_ready = all([wp_base, wp_user, wp_pass])

    try:
        detected_keys = ", ".join(sorted(list(getattr(st, "secrets", {}).keys())))
    except Exception:
        detected_keys = "(لا يمكن قراءة st.secrets)"
    st.caption("🔍 مفاتيح موجودة في st.secrets: " + detected_keys)
    st.caption(
        f"WP_BASE_URL: {'OK' if wp_base else 'MISSING'} · "
        f"WP_USER: {'OK' if wp_user else 'MISSING'} · "
        f"WP_APP_PASS: {'OK' if wp_pass else 'MISSING'}"
    )

    def slugify(name: str) -> str:
        s = ''.join(c for c in unicodedata.normalize('NFKD', name) if not unicodedata.combining(c))
        import re as _re
        s = _re.sub(r'\W+', '_', s).strip('_').lower()
        return s or "custom"

    current_title = st.session_state.get("last_title") or ""
    default_slug = slugify(current_title) if current_title else slugify(st.session_state.get('last_article_md', '')[:40] or "article")

    pcol1, pcol2 = st.columns([2,1])
    with pcol1:
        wp_slug = st.text_input("Slug (اختياري)", default_slug)
        wp_status = st.selectbox("الحالة", ["draft", "pending", "publish"], index=0)
    with pcol2:
        cattxt = st.text_input("IDs للتصنيفات (اختياري، مفصولة بفواصل)", "")
        tagtxt = st.text_input("IDs للوسوم (اختياري، مفصولة بفواصل)", "")

    def wp_publish_draft(title: str, markdown_body: str, slug: str = None,
                         categories=None, tags=None, status: str = "draft") -> dict:
        base = (_get_secret_fuzzy("WP_BASE_URL", ("WP_URL","WORDPRESS_BASE_URL")) or "").rstrip("/")
        user = _get_secret_fuzzy("WP_USER", ("WORDPRESS_USER","WP_USERNAME"))
        app_pass = _get_secret_fuzzy("WP_APP_PASS", ("WP_APP_PASSWORD","WORDPRESS_APP_PASSWORD"))
        if not base or not user or not app_pass:
            raise RuntimeError("بيانات ووردبريس ناقصة: WP_BASE_URL / WP_USER / WP_APP_PASS")
        html = md.markdown(markdown_body or "", extensions=["extra", "sane_lists"])
        url = f"{base}/wp-json/wp/v2/posts"
        payload = {"title": title or "بدون عنوان", "content": html, "status": status}
        if slug: payload["slug"] = slug
        if categories: payload["categories"] = categories
        if tags: payload["tags"] = tags
        resp = requests.post(url, json=payload, auth=(user, app_pass), timeout=45)
        resp.raise_for_status()
        return resp.json()

    if not wp_ready:
        st.info("المفاتيح ناقصة أو فارغة. أضف WP_BASE_URL و WP_USER و WP_APP_PASS إلى secrets.toml ثم اضغط Restart.")
    else:
        if st.button("🚀 نشر كمسودة على ووردبريس"):
            article_md_to_publish = st.session_state.get('last_article_md', '')
            if not article_md_to_publish.strip():
                st.warning("لا يوجد نص مقال لنشره. أنشئ المقال أولًا.")
            else:
                try:
                    cats = [int(x) for x in cattxt.split(",") if x.strip().isdigit()] if cattxt.strip() else None
                    tags = [int(x) for x in tagtxt.split(",") if x.strip().isdigit()] if tagtxt.strip() else None
                    res = wp_publish_draft(
                        title=current_title or "مقال جديد",
                        markdown_body=article_md_to_publish,
                        slug=wp_slug or None,
                        categories=cats,
                        tags=tags,
                        status=wp_status,
                    )
                    st.success(f"تم إنشاء منشور (ID={res.get('id')}) بحالة {res.get('status')}.")
                    link = res.get("link") or (res.get("guid") or {}).get("rendered")
                    if link:
                        st.markdown(f"[فتح في ووردبريس]({link})")
                except Exception as e:
                    st.error(f"فشل النشر: {e}")

# ------------------ Tab 2: Competitor Analysis ------------------
with tab_comp:
    st.subheader("تحليل أول منافسين — روابط يدوية (بدون API)")
    st.markdown("أدخل رابطين للصفحات المتصدّرة. سنجلب المحتوى ونحلّله من زاوية المحتوى وE-E-A-T فقط.")
    query = st.text_input("استعلام البحث", "أفضل مطاعم دبي مول")
    place_scope_desc = st.text_input("وصف النطاق/المكان (اختياري)", "داخل دبي مول فقط")
    url_a = st.text_input("رابط المنافس A", "")
    url_b = st.text_input("رابط المنافس B", "")

    tone_for_analysis = st.selectbox("نبرة التحليل",
        ["ناقد صارم | مراجعات الجمهور", "ناقد صارم | تجربة مباشرة + مراجعات", "دليل تحريري محايد"], index=0)
    reviews_weight_analysis = st.slider("وزن الاعتماد على المراجعات (٪) في التحليل", 0, 100, 60, step=5)

    colx, coly = st.columns(2)
    with colx: fetch_btn = st.button("📥 جلب المحتوى")
    with coly: analyze_btn = st.button("🧠 تنفيذ التحليل")

    if fetch_btn:
        if not url_a or not url_b:
            st.warning("أدخل رابطين أولًا.")
        else:
            try:
                with st.spinner("جلب الصفحة A..."):
                    page_a = fetch_and_extract(url_a)
                with st.spinner("جلب الصفحة B..."):
                    page_b = fetch_and_extract(url_b)
                st.session_state["comp_pages"] = {"A": page_a, "B": page_b}
                st.success("تم الجلب والتهيئة.")
                st.write("**A:**", page_a.get("title") or url_a, f"({page_a['word_count']} كلمة)")
                st.write("**B:**", page_b.get("title") or url_b, f"({page_b['word_count']} كلمة)")
                st.caption("يمكنك الآن الضغط على زر تنفيذ التحليل.")
            except Exception as e:
                st.error(f"تعذّر الجلب: {e}")

    if analyze_btn:
        if not _has_api_key():
            st.error("لا يوجد OPENAI_API_KEY.")
            st.stop()
        pages = st.session_state.get("comp_pages")
        if not pages:
            st.warning("الرجاء جلب المحتوى أولًا.")
        else:
            client = get_client()
            try:
                with st.spinner("يشغّل التحليل..."):
                    analysis_md = analyze_competitors(client, primary_model, fallback_model, pages["A"], pages["B"], query, place_scope_desc or "—", tone_for_analysis, reviews_weight_analysis)
                st.session_state["comp_analysis_md"] = analysis_md
                st.subheader("📊 تقرير التحليل"); st.markdown(analysis_md)
                gaps = extract_gap_points(analysis_md)
                if gaps:
                    st.info("تم استخراج توصيات Gap-to-Win — يمكنك حقنها في برومبت المقال.")
                    st.text_area("التوصيات المستخرجة (قابلة للتحرير قبل الحقن)", gaps, key="comp_gap_notes", height=160)
                else:
                    st.warning("لم يتم العثور على قسم 'Gap-to-Win'. انسخه يدويًا.")
            except Exception as e:
                st.error(f"تعذّر التحليل: {e}")

# ------------------ Tab 3: QC ------------------
with tab_qc:
    st.subheader("🧪 فحص بشرية وجودة المحتوى")
    qc_text = st.text_area("الصق نص المقال هنا", st.session_state.get("last_article_md",""), height=300)
    col_q1, col_q2, col_q3 = st.columns(3)
    with col_q1:
        do_fluff = st.checkbox("كشف الحشو والعبارات القالبية", value=True)
    with col_q2:
        do_eeat = st.checkbox("مؤشرات E-E-A-T", value=True)
    with col_q3:
        do_llm_review = st.checkbox("تشخيص مُرشد (LLM)", value=True)

    if st.button("🔎 تحليل سريع"):
        if not qc_text.strip():
            st.warning("الصق النص أولًا.")
        else:
            rep = quality_report(qc_text)
            st.session_state["qc_report"] = rep
            st.markdown("### بطاقة الدرجات")
            colA, colB, colC = st.columns(3)
            with colA: st.metric("Human-style Score", rep["human_style_score"])
            with colB: st.metric("Sensory Ratio", rep["sensory_ratio"])
            with colC: st.metric("Fluff Density", rep["fluff_density"])
            st.markdown("#### تنوّع الجمل"); st.json(rep["sentence_variety"])
            if do_eeat:
                st.markdown("#### E-E-A-T"); st.json({"presence": rep["eeat"], "score": rep["eeat_score"]})
                st.markdown("#### Information Gain"); st.json({"score": rep["info_gain_score"]})
            if do_fluff:
                st.markdown("#### عبارات قالبية مرصودة")
                boiler = rep.get("boilerplate_flags") or []
                if boiler:
                    for f in boiler:
                        pattern = f.get("pattern", "?")
                        excerpt = f.get("excerpt", "")
                        st.write(f"- **نمط:** `{pattern}` — مقتطف: …{excerpt}…")
                else:
                    st.caption("لا توجد عبارات قالبية ظاهرة.")

                repeats = rep.get("repeated_phrases") or []
                if repeats:
                    st.markdown("#### عبارات متكررة بشكل زائد")
                    for g, c in repeats:
                        st.write(f"- `{g}` × {c}")
            st.success("انتهى التحليل السريع.")
            st.session_state["qc_text"] = qc_text

    if do_llm_review and st.button("🧠 تشخيص مُرشد (LLM)"):
        if not qc_text.strip():
            st.warning("الصق النص أولًا.")
        elif not _has_api_key():
            st.error("لا يوجد OPENAI_API_KEY.")
        else:
            client = get_client()
            out = llm_review(client, primary_model, fallback_model, qc_text)
            st.markdown("### تقرير المُراجع"); st.markdown(out)
            st.session_state["qc_review_md"] = out

    st.markdown("---")
    st.markdown("#### إصلاح ذكي للأجزاء المعلّمة")
    flagged_block = st.text_area("ألصق الأسطر التي تريد تحسينها (سطر لكل مقطع)", height=140, placeholder="انسخ المقاطع الضعيفة وضعها هنا…")
    if st.button("✍️ أعِد الصياغة للمقاطع المحددة فقط"):
        if not flagged_block.strip():
            st.warning("أدخل المقاطع أولًا.")
        elif not qc_text.strip():
            st.warning("لا يوجد نص أساس لإعادة الكتابة.")
        elif not _has_api_key():
            st.error("لا يوجد OPENAI_API_KEY.")
        else:
            client = get_client()
            new_text = llm_fix(client, primary_model, fallback_model, qc_text, flagged_block.splitlines())
            st.markdown("### النص بعد الإصلاح"); st.markdown(new_text)
            st.session_state["last_article_md"] = new_text
            st.success("تم الإصلاح الموضعي.")

# ------------------ Tab 4: Google Places (الجديد) ------------------
with tab_places:
    st.subheader("ابحث عن مطاعم عبر Google Places")

    # ملاحظة: تأكد أنك وضعت places_core.py في utils/integrations/
    from utils.integrations.places_core import (
        CITY_PRESETS,
        places_search_text,
        make_items_from_places,
    )

    # مفتاح Google من الأسرار
    api_key = st.secrets.get("GOOGLE_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        st.error("GOOGLE_API_KEY غير موجود في secrets.")
        st.stop()

    query = st.text_input("🔎 استعلام البحث (مثال: برجر بالرياض، بخاري جدة...)")
    city_key = st.selectbox("🏙️ اختر مدينة", list(CITY_PRESETS.keys()))
    max_results = st.number_input("🔢 الحد الأقصى للنتائج", min_value=1, max_value=20, value=10)
    min_reviews = st.number_input("⭐ الحد الأدنى لعدد المراجعات", min_value=0, value=50)

    if st.button("🚀 جلب النتائج"):
        if not query:
            st.warning("اكتب استعلامًا أولًا.")
        else:
            places = places_search_text(
                api_key,
                query,
                city_key,
                max_results=int(max_results),
            )

            region_code = CITY_PRESETS[city_key].get("regionCode", "SA")
            items_raw = make_items_from_places(
                api_key,
                places,
                min_reviews=int(min_reviews),
                region_code=region_code,
            )

            def convert_item(r: dict) -> dict:
                return {
                    "name": r.get("name", ""),
                    "address": r.get("address"),
                    "phone": r.get("phone"),
                    "thursday_hours": r.get("thursday_hours"),
                    "family_friendly": r.get("family_friendly") or "نعم (تقديري)",
                    "price_per_person": r.get("price_range"),
                    "signature_dish": r.get("signature_dish") or "—",
                    "busy_times": r.get("crowd_note"),
                    "maps_url": r.get("maps_uri"),
                    "website": r.get("website"),
                }

            items = [convert_item(r) for r in items_raw]

            st.session_state["places_items"] = items
            st.session_state["places_index"] = { normalize_ar(it["name"]): it for it in items }

            st.success(f"تم جلب {len(items)} مطعمًا.")
            if items:
                try:
                    import pandas as pd
                    df = pd.DataFrame([{
                        "الاسم": it["name"],
                        "العنوان": it["address"] or "غير متوفر",
                        "السعر للشخص": it["price_per_person"] or "غير متوفر",
                        "أوقات الزحمة": it["busy_times"] or "غير متوفر"
                    } for it in items])
                    st.dataframe(df, use_container_width=True)
                except Exception:
                    st.write("**أول 5 نتائج:**")
                    for it in items[:5]:
                        st.write("•", it["name"], "—", (it["address"] or "غير متوفر"))

    if "places_items" in st.session_state and st.session_state["places_items"]:
        if st.button("➕ أضِف الأسماء إلى حقل التوليد"):
            restaurants_text = "\n".join([it["name"] for it in st.session_state["places_items"]])
            st.session_state["restaurants_text"] = restaurants_text
            st.success("تمت إضافة الأسماء إلى تبويب توليد المقال ✍️ — افتحه الآن.")
