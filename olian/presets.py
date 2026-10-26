"""Starter rule material (edit freely in the panel). Intent phrases say 'the author wants something'; the topic comes from you."""
INTENT = {
    "uk": ["шукаю", "шукаємо", "потрібен", "потрібна", "потрібні", "потрібно", "підкажіть", "порадьте", "хто може",
           "хто знає", "де можна", "де купити", "де замовити", "хочу купити", "хочу замовити", "куплю", "замовлю", "скільки коштує"],
    "ru": ["ищу", "ищем", "нужен", "нужна", "нужны", "нужно", "подскажите", "посоветуйте", "кто может", "кто знает",
           "где можно", "где купить", "где заказать", "хочу купить", "хочу заказать", "куплю", "закажу", "сколько стоит"],
    "en": ["looking for", "need a", "need an", "i need", "we need", "anyone know", "can anyone", "can someone", "recommend",
           "where can i", "how much", "want to buy", "want to order"],
    "cs": ["hledám", "hledáme", "potřebuji", "potřebujeme", "doporučte", "kdo umí", "kde koupit", "kolik stojí"],
}
EXCLUDE = ["продам", "продаю", "пропоную", "пропонуємо", "предлагаю", "предлагаем", "for sale", "we offer", "i offer",
           "nabízím", "nabízíme", "промокод", "подписывайтесь", "підписуйтесь"]


def intent_phrases(langs: list[str]) -> list[str]:
    out: list[str] = []
    for l in langs:
        out += INTENT.get(l.strip(), [])
    return out
