"""Reflection quotes — the 'self-reflex' philosophical lines, single-sourced.

Previously these lived as hardcoded bash arrays in softwares/bin/magnolia-selfreflex.
They now live here so any distillation path (inline, timer, startup, manual) can
surface one, and magnolia-selfreflex calls into this module instead of carrying
its own copy.
"""

import random

OPENING_QUOTES = [
    "吾日三省吾身 — I examine myself three times a day.",
    "学而不思则罔，思而不学则殆 — Learning without thought is labor lost; thought without learning is perilous.",
    "见贤思齐焉，见不贤而内自省也 — When you see someone of virtue, think of equaling them; when you see someone without virtue, examine your own heart.",
    "知之为知之，不知为不知，是知也 — To know what you know and know what you do not know is true knowledge.",
    "温故而知新，可以为师矣 — By reviewing the old, one learns the new.",
    "工欲善其事，必先利其器 — The craftsman who wishes to do his work well must first sharpen his tools.",
    "博学之，审问之，慎思之，明辨之，笃行之 — Learn extensively, inquire accurately, think carefully, discriminate clearly, practice devotedly.",
    "不积跬步，无以至千里 — Without accumulating small steps, one cannot reach a thousand li.",
    "知人者智，自知者明 — Knowing others is wisdom; knowing yourself is enlightenment.",
    "千里之行，始于足下 — A journey of a thousand li begins with a single step.",
    "天行健，君子以自强不息 — As Heaven maintains vigor through movement, a gentleman ceaselessly strives for self-improvement.",
    "地势坤，君子以厚德载物 — As Earth is receptive and generous, a gentleman builds broad capacity to carry all things.",
    "君子务本，本立而道生 — A gentleman focuses on the root; once the root is established, the way grows.",
    "欲速则不达，见小利则大事不成 — Haste makes waste; preoccupation with petty gains prevents great accomplishments.",
    "三人行，必有我师焉 — When three walk together, there must be one who can be my teacher.",
    "敏而好学，不耻下问 — Quick to learn and not ashamed to ask those below.",
    "士不可以不弘毅，任重而道远 — A scholar must be broad-minded and resolute, for his burden is heavy and his road is long.",
    "穷则变，变则通，通则久 — When cornered, change; by changing, you find a way through; through that way, you endure.",
    "苟日新，日日新，又日新 — If you can improve in one day, do so every day, and again the next.",
    "凡事豫则立，不豫则废 — In all things, preparation brings success; lack of preparation brings failure.",
    "天下难事，必作于易；天下大事，必作于细 — All difficult things begin as easy things; all great things begin as small things.",
    "上善若水，水善利万物而不争 — The highest good is like water; water benefits all things and does not contend.",
    "吾生也有涯，而知也无涯 — Life is finite, but knowledge is infinite.",
    "君子和而不同 — A gentleman seeks harmony but not sameness.",
    "路漫漫其修远兮，吾将上下而求索 — The road ahead is long and far; I will search high and low.",
]

CLOSING_QUOTES = [
    "以铜为镜，可以正衣冠。以古为镜，可以知兴替。以人为镜，可以明得失。 — With bronze as a mirror, one adjusts his robes; with antiquity as a mirror, one knows rise and fall; with people as a mirror, one sees gain and loss.",
    "知错能改，善莫大焉 — There is no greater good than the ability to correct one's errors.",
    "君子之过也，如日月之食焉：过也，人皆见之；更也，人皆仰之。 — The faults of a gentleman are like eclipses of the sun and moon: all see them; when corrected, all look up to him.",
    "慎终如始，则无败事 — If one is as careful at the end as at the beginning, there will be no failure.",
    "熟能生巧 — Proficiency comes from familiarity.",
    "前车之覆，后车之鉴 — The overturned cart ahead is a warning to the cart behind.",
    "亡羊补牢，未为迟也 — It is not too late to mend the pen after the sheep are lost.",
    "实事求是 — Seek truth from facts.",
    "天道酬勤 — Heaven rewards diligence.",
    "精诚所至，金石为开 — Complete devotion can open metal and stone.",
    "纸上得来终觉浅，绝知此事要躬行 — What is learned from books is shallow after all; to truly understand, one must practice.",
    "宝剑锋从磨砺出，梅花香自苦寒来 — The sword's edge is honed by grinding; the plum blossom's fragrance comes from bitter cold.",
    "业精于勤，荒于嬉；行成于思，毁于随 — Excellence comes from diligence and is ruined by frivolity; success comes from thought and is ruined by carelessness.",
    "问渠那得清如许，为有源头活水来 — How is the stream so clear? Because fresh water flows from its source.",
    "不畏浮云遮望眼，自缘身在最高层 — Unafraid that floating clouds may block my sight, for I stand on the highest level.",
    "合抱之木，生于毫末；九层之台，起于累土 — A tree that fills the arms grows from a tiny sprout; a nine-story terrace rises from a basket of earth.",
    "博学而笃志，切问而近思，仁在其中矣 — Study widely, hold fast to your purpose, inquire earnestly, and reflect closely — benevolence lies in these.",
    "知之者不如好之者，好之者不如乐之者 — Those who know are not as good as those who love; those who love are not as good as those who delight in it.",
    "逝者如斯夫，不舍昼夜 — Time flows like this river, day and night without ceasing.",
    "己所不欲，勿施于人 — Do not impose on others what you yourself do not desire.",
    "人法地，地法天，天法道，道法自然 — Man follows Earth, Earth follows Heaven, Heaven follows the Way, and the Way follows what is natural.",
    "大直若屈，大巧若拙，大辩若讷 — Great straightness seems bent; great skill seems clumsy; great eloquence seems tongue-tied.",
    "慎终追远，民德归厚矣 — When the end is carefully attended to and the distant past is remembered, the people's virtue grows deep.",
]


def pick_quote(kind: str = "opening") -> str:
    """Return a random reflection quote. kind is 'opening' or 'closing';
    anything else falls back to 'opening'."""
    pool = CLOSING_QUOTES if kind == "closing" else OPENING_QUOTES
    return random.choice(pool)
