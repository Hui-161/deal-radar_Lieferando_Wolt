"""Shared food knowledge: cuisine categories, diet and item-kind detection, text folding."""
import math
import re
import unicodedata
from typing import Optional

# Canonical cuisine categories: key, label, emoji, Lieferando cuisine keys, Wolt filter ids, Wolt search term
CUISINES = [
    ("pizza", "Pizza", "🍕", {"italian-style-pizza", "italienische-pizza", "amerikanische-pizza", "pizza"}, {"pizza"}, "pizza"),
    ("burger", "Burger", "🍔", {"burger"}, {"burgers", "burger"}, "burger"),
    ("doener", "Döner & Kebab", "🥙", {"doener", "gyros"}, {"doner", "kebab", "gyros"}, "döner"),
    ("asiatisch", "Asiatisch", "🥢", {"asiatisch-essen", "nudeln", "indonesisches-essen"}, {"asian", "noodles"}, "asiatisch"),
    ("sushi", "Sushi & Japanisch", "🍣", {"sushi", "japanisches-essen"}, {"sushi", "japanese", "ramen", "udon", "yakitori"}, "sushi"),
    ("italienisch", "Pasta & Italienisch", "🍝", {"italienisch", "italian-food", "pasta"}, {"italian", "pasta"}, "pasta"),
    ("indisch", "Indisch & Curry", "🍛", {"indisch", "indian-food", "pakistanisches-essen", "curry"}, {"indian", "curry", "nepalese"}, "curry"),
    ("vietnamesisch", "Vietnamesisch", "🍜", {"vietnamesisch"}, {"vietnamese", "pho"}, "pho"),
    ("chinesisch", "Chinesisch", "🥡", {"chinesisch", "chinese-food", "sichuan", "dumplings"}, {"chinese", "szechuan", "dumplings"}, "chinesisch"),
    ("thai", "Thailändisch", "🌶️", {"thailaendisch"}, {"thai"}, "thai"),
    ("koreanisch", "Koreanisch", "🍲", {"koreanisches-essen"}, {"korean"}, "korean"),
    ("tuerkisch", "Türkisch", "🫓", {"tuerkisches-essen", "tuerkische-pizza"}, {"turkish"}, "türkisch"),
    ("orientalisch", "Orientalisch & Falafel", "🧆",
     {"arabisches-essen", "libanesisches-essen", "syrische-gerichte", "persisches-essen", "israelisches-essen", "falafel", "afghanisch-essen"},
     {"arabic", "falafel", "hummus", "persian", "israeli", "lebanese"}, "falafel"),
    ("griechisch", "Griechisch", "🫒", {"griechisches-essen"}, {"greek"}, "griechisch"),
    ("mexikanisch", "Mexikanisch", "🌮", {"mexikanisch", "lateinamerikanisch", "argentinisch-essen", "brasilianisch-essen"},
     {"mexican", "latin-american", "burrito", "taco"}, "burrito"),
    ("haehnchen", "Hähnchen", "🍗", {"gefluegelgerichte", "haehnchen"}, {"chicken", "wings"}, "chicken"),
    ("grill", "Steak & Grill", "🥩", {"steaks", "rindfleisch", "spare-ribs", "kalb", "schweinefleisch", "amerikanisches-essen", "hot-dog"},
     {"steak", "bbq", "american", "grill"}, "steak"),
    ("bowls", "Bowls & Salate", "🥗", {"bowls", "poke-bowl", "salat", "suppen"}, {"bowl", "salad", "healthy", "poke", "soup"}, "bowl"),
    ("sandwiches", "Sandwiches & Wraps", "🥪", {"sandwiches", "wraps", "bagels"}, {"sandwich", "wraps", "bagel"}, "sandwich"),
    ("deutsch", "Deutsch", "🥨", {"deutsches-essen", "deutsche-gerichte", "hausmannskost", "schnitzel", "oesterreichische-kuche", "flammkuchen"},
     {"german", "austrian", "schnitzel"}, "schnitzel"),
    ("fisch", "Fisch", "🐟", {"fisch", "meeresfruchte-essen"}, {"fish", "seafood"}, "fisch"),
    ("fruehstueck", "Frühstück & Café", "🥐", {"fruehstueck", "bei-cafes", "backwaren"}, {"breakfast", "brunch", "cafe", "bakery"}, "frühstück"),
    ("dessert", "Süßes & Eis", "🍰", {"desserts", "torte", "eiscreme", "donuts", "pfannkuchen"},
     {"dessert", "ice_cream", "waffles", "crepe"}, "dessert"),
    ("shops", "Geschäfte", "🛒", {"lebensmittel", "groceries", "shops", "geschaefte", "alkohol", "blumen", "apotheken"},
     {"grocery", "retail"}, None),
]
CUISINE_BY_KEY = {c[0]: c for c in CUISINES}
_L_MAP = {lk: c[0] for c in CUISINES for lk in c[3]}
_W_MAP = {wk: c[0] for c in CUISINES for wk in c[4]}
SHOP_KEYS = CUISINES[-1][3]

# Lieferando "cuisines" that are really marketing labels, not food types
_L_NOT_A_CUISINE = {"offers", "angebote", "2-fuer-1-deals", "football-deals", "mittagessen", "meal_deal", "100-prozent-halal",
                    "halal", "vegan-essen", "vegetarisches-essen", "glutenfreies-essen", "bioessen"}


def lieferando_cuisines(unique_names: list[str]) -> list[str]:
    return sorted({_L_MAP[u] for u in unique_names if u in _L_MAP})


def wolt_cuisines(filter_ids: list[str]) -> list[str]:
    return sorted({_W_MAP[f] for f in filter_ids if f in _W_MAP})


def lieferando_tags(cuisines: list[dict]) -> list[str]:
    """Readable cuisine names for a card (deduplicated, marketing labels dropped)."""
    seen, out = set(), []
    for c in cuisines:
        name = (c.get("name") or "").strip()
        if not name or c.get("uniqueName") in _L_NOT_A_CUISINE or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out[:4]


# Cuisine from a restaurant name, for providers without cuisine data per restaurant (Uber Eats feed)
_NAME_CUISINES = [
    ("pizza", r"pizz"),
    ("burger", r"burger|smash"),
    ("doener", r"doner|kebab|kebap|gyros|durum"),
    ("sushi", r"sushi|ramen|japan|izakaya|udon"),
    ("vietnamesisch", r"\bpho\b|viet|banh|saigon|hanoi"),
    ("thai", r"\bthai|bangkok"),
    ("koreanisch", r"korea|bibimbap|kimchi|seoul"),
    ("chinesisch", r"china|chines|wok\b|dumpling|dim sum|sichuan|szechuan"),
    ("asiatisch", r"asia|noodle|nudel"),
    ("indisch", r"india|indisch|curry|tandoor|masala|punjab|bombay|delhi|nepal"),
    ("orientalisch", r"falafel|shawarma|schawarma|hummus|liban|leban|syri|persi|arab|oriental"),
    ("griechisch", r"griech|greek|souvlaki|taverna"),
    ("mexikanisch", r"taco|burrito|mexi|cantina"),
    ("haehnchen", r"chicken|hahnchen|huhn|wings|broiler|hendl"),
    ("italienisch", r"pasta|trattoria|osteria|italia|ristorante"),
    ("bowls", r"poke|bowl|salat|salad"),
    ("sandwiches", r"sandwich|bagel|wrap|subway"),
    ("fruehstueck", r"backer|bakery|backerei|cafe|coffee|kaffee|fruhstuck|brunch"),
    ("dessert", r"\beis\b|gelato|ice cream|dessert|waffel|donut|crepe|cake|kuchen"),
    ("grill", r"steak|grill|bbq"),
    ("deutsch", r"schnitzel|currywurst|wirtshaus"),
]
_NAME_CUISINES = [(key, re.compile(pattern)) for key, pattern in _NAME_CUISINES]
_SHOP_NAME = re.compile(r"spati|spatkauf|kiosk|market|markt\b|supermarkt|getranke|rewe|flink|edeka|lidl|aldi|penny|netto"
                        r"|apotheke|blumen|drogerie|\bdm\b|rossmann|tabak|liquor|vape|kaufhaus|night store")


def guess_cuisines(text: str) -> list[str]:
    t = fold(text)
    return sorted({key for key, pattern in _NAME_CUISINES if pattern.search(t)})


def looks_like_shop(name: str) -> bool:
    return bool(_SHOP_NAME.search(fold(name)))


def deal_type_from_text(text: str) -> str:
    """Deal type for providers that only give a text ('2 für 1 Deal', '5 € Ersparnis ab 25 €' ...)."""
    t = fold(text)
    if re.search(r"2\s?(fur|for|4)\s?1|kaufe 1\b.*(\b1\b|gratis|kostenlos)|buy 1 get 1|bogo", t):
        return "bogof"
    if "liefergebuhr" in t or "lieferkosten" in t or "lieferung" in t:
        return "free_delivery"
    if re.search(r"\bab \d", t) or "warenkorb" in t or "bestellung" in t or "ersparnis" in t:
        return "basket_discount"
    if "%" in t or "rabatt" in t:
        return "item_discount"
    if "gratis" in t or "kostenlos" in t:
        return "free_item"
    return "other"


def euro_cents(text: str) -> Optional[int]:
    """'9,04 €' -> 904; None if the text holds no amount."""
    m = re.search(r"(\d{1,4})[,.](\d{2})", text or "")
    return int(m.group(1)) * 100 + int(m.group(2)) if m else None


def fold(text: Optional[str]) -> str:
    """Lowercase without accents/umlauts so that 'Döner', 'doener' and 'doner' compare equal."""
    s = unicodedata.normalize("NFKD", (text or "").lower().replace("ß", "ss"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("ae", "a").replace("oe", "o").replace("ue", "u")


# --- Diet detection (all patterns work on folded text) ---
_VEGAN_NEG = re.compile(r"(nicht|kein|non)[- ]?vegan")
_VEGAN_OPT = re.compile(r"vegane? (option|variante|alternative)|vegan (auf wunsch|moglich|erhaltlich|gegen aufpreis)|auch vegan|als vegan|optional vegan")
_VEGAN = re.compile(r"\bvegan|plant[- ]?based|pflanzlich")
_VEGGIE = re.compile(r"vegetar|veggie|\bveggi|\(v\)|\bchay\b|fleischlos|ohne fleisch")
_MEAT = re.compile(
    r"fleisch|\brind|beef|schwein|pork|\bspeck|bacon|hahnchen|\bhuhn|huhnchen|huhner|chicken|geflugel|\bpute|truthahn|turkey"
    r"|\blamm|lamb|\bkalb|vitello|\bente\b|\benten|duck|wurst|salami|schinken|\bham\b|prosciutto|parma|pancetta|guanciale"
    r"|mortadella|nduja|chorizo|pepperoni|sucuk|sujuk|doner|kebab|kebap|gyros|shawarma|schawarma|\bhack|bolognese|carbonara"
    r"|ragu|steak|patty|\bfisch|fish|lachs|salmon|thunfisch|\btuna|tonno|garnele|shrimp|prawn|scampi|krabbe|meeresfrucht"
    r"|seafood|frutti di mare|calamar|tintenfisch|oktopus|octopus|muschel|sardell|anchovi|sardine|surimi|\bkrebs|hummer"
    r"|lobster|\baal\b|unagi|\bebi\b|\bsake\b|maguro|ikura|tobiko|\bcarne|pollo|manzo|maiale|gamberi|wings|nugget|cevapcici"
    r"|frikadelle|leberkas|kofte|lahmacun|pastrami|roastbeef|brisket|pulled|bulgogi|katsu|karaage|yakitori|gyoza|\bpho\b"
    r"|hawaii|diavol|capricciosa|calzone|regina|quattro stagioni|bbq|schnitzel|cordon|gulasch|\bmett\b|\bwild\b|hirsch|\breh\b"
)
_VEG_HINT = re.compile(
    r"margherita|margarita|marinara|funghi|pilz|champignon|mushroom|kase|cheese|mozzarella|burrata|ricotta|parmesan|pecorino"
    r"|\bfeta|halloumi|paneer|tofu|tempeh|seitan|falafel|hummus|\bhumus|gemuse|vegetable|spinat|spinach|aubergine|melanzane"
    r"|zucchini|kartoffel|potato|pommes|fries|frites|linsen|lentil|\bdal\b|daal|kichererbse|chickpea|bohnen|avocado|guacamole"
    r"|quinoa|caprese|pesto|arrabbiata|aglio|pomodoro|napoli|pfannkuchen|pancake|waffel|waffle|crepe|kuchen|\bcake|brownie"
    r"|cookie|tiramisu|panna cotta|sorbet|\beis\b|ice cream|edamame|kimchi|fruhlingsrolle|spring roll"
)
# Dishes that usually contain meat without saying so - no guessing there ("Chili Cheese Menü")
_NO_GUESS = re.compile(r"burger|\bmenu|combo|smash|bucket|hot ?dog|\bdog\b|kumpir|\bbao\b")
_TWO_FOR_ONE = re.compile(r"\b2\s?(for|fur|4)\s?1\b|zwei zum preis von einem|buy one get one|\bbogo")


def detect_diet(name: str, desc: str = "", category: str = "", labels=(), restaurant_name: str = "") -> tuple[Optional[str], Optional[str]]:
    """
    Returns (diet, source): diet is 'vegan', 'vegetarian' or None;
    source is 'label' (provider flag), 'text' (stated in name/description/category),
    'option' (vegan variant on request) or 'guess' (no meat/fish words, typical veggie ingredients).
    """
    labels = {str(lb).lower() for lb in labels or ()}
    if any("vegan" in lb for lb in labels):
        return "vegan", "label"
    if any("vegetar" in lb for lb in labels):
        return "vegetarian", "label"

    name_f, desc_f = fold(name), fold(desc)
    item_text = f"{name_f} {desc_f}"
    cat = fold(category)
    # "Poke Bowl Crispy Chicken" with "veganes Dressing" in the description is not vegan
    meat_in_name = bool(_MEAT.search(name_f)) and not _VEGAN.search(name_f) and not _VEGGIE.search(name_f)
    if not _VEGAN_NEG.search(item_text):
        if _VEGAN_OPT.search(item_text):
            return "vegan", "option"
        if _VEGAN.search(name_f) or (_VEGAN.search(desc_f) and not meat_in_name):
            return "vegan", "text"
    if _VEGGIE.search(name_f) or (_VEGGIE.search(desc_f) and not meat_in_name):
        return "vegetarian", "text"
    if not meat_in_name:
        if _VEGAN.search(cat) and not _VEGGIE.search(cat):
            return "vegan", "text"
        if _VEGGIE.search(cat) or _VEGAN.search(cat):
            return "vegetarian", "text"

    if _MEAT.search(item_text) or _MEAT.search(cat) or _NO_GUESS.search(item_text):
        return None, None
    if _VEGAN.search(fold(restaurant_name)):
        return "vegan", "guess"
    if _VEG_HINT.search(item_text):
        return "vegetarian", "guess"
    return None, None


# --- Item kind: food, drink or extra (dips, sauces, cutlery ...) ---
_EXTRA = re.compile(r"\bdips?\b|sauce|sosse|dressing|\bextras?\b|zusatz|topping|besteck|cutlery|\btute\b|pfand")
_DRINK = re.compile(
    r"getrank|drink|beverage|softdrink|limonade|\bsafte?\b|\bbier\b|\bbiere\b|\bpils|radler|\bbeer|\bweine?\b|rotwein|weisswein"
    r"|wine|spirituos|alkohol|cocktail|kaffee|coffee|\btee\b|\btea\b|smoothie|shake|wasser\b|water\b|\bcola\b|bubble tea"
    r"|\blassi\b"
)
_FOODISH = re.compile(r"kuchen|cake|snack|speise|essen|food|gericht|fruhstuck|brot|backwar|dessert|suppe|soup|bowl")
_VOLUME = re.compile(r"\b\d+([,.]\d+)?\s?(l|ml|cl|liter)\b")


def detect_kind(name: str, category: str = "", labels=(), price_cents: Optional[int] = None) -> str:
    if price_cents is not None and price_cents < 50:
        return "extra"
    cat, nm = fold(category), fold(name)
    if _EXTRA.search(cat):
        return "extra"
    if "alcohol" in {str(lb).lower() for lb in labels or ()}:
        return "drink"
    if _DRINK.search(cat) and not _FOODISH.search(cat):
        return "drink"
    # Only short names count as extras: "Knoblauchsauce" yes, "Spaghetti mit Tomatensauce" no
    if len(nm.split()) <= 2 and _EXTRA.search(nm):
        return "extra"
    if not _FOODISH.search(nm) and (_VOLUME.search(nm) or _DRINK.search(nm)):
        return "drink"
    return "food"


def distance_m(lat1, lon1, lat2, lon2) -> Optional[int]:
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(6371000 * 2 * math.asin(math.sqrt(a)))


def eta_min(eta: str) -> Optional[int]:
    m = re.match(r"\s*(\d+)", eta or "")
    return int(m.group(1)) if m else None


def make_item(item_id, name, desc, price_cents, orig_price_cents, img, category, labels=(), restaurant_name="") -> dict:
    """Normalized menu item; fields starting with '_' never leave the server."""
    name = (name or "").strip()
    desc = re.sub(r"\s+", " ", (desc or "").strip())
    if len(desc) > 220:
        desc = desc[:217].rstrip() + " …"
    diet, diet_src = detect_diet(name, desc, category, labels, restaurant_name)
    # Deal items such as "2for1 - Single Smash Burger" are often plain menu items
    two_for_one = _TWO_FOR_ONE.search(fold(f"{name} {category}"))
    base_deal = {"type": "bogof", "text": "2 für 1", "pct": None} if two_for_one else None
    return {
        "id": str(item_id),
        "name": name,
        "desc": desc,
        "price_cents": price_cents,
        "orig_price_cents": orig_price_cents if orig_price_cents and price_cents and orig_price_cents > price_cents else None,
        "img": img,
        "cat": (category or "").strip(),
        "diet": diet,
        "diet_src": diet_src,
        "kind": detect_kind(name, category, labels, price_cents),
        "deal": base_deal,
        "_deal0": base_deal,  # deal without provider offers; offers are re-applied on top of it
        "_n": fold(name),
        "_h": fold(f"{name} {desc} {category}") + {"vegan": " vegan vegetarisch", "vegetarian": " vegetarisch"}.get(diet, ""),
    }


def public_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}
