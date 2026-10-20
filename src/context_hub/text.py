"""Tokenization tuned for mixed Korean/English notes without external models."""

from __future__ import annotations

import re

_WORD = re.compile(r"[0-9a-z_][0-9a-z_.\-]*[0-9a-z_]|[0-9a-z_]|[가-힣]+")
_PARTICLES = sorted(
    "은 는 이 가 을 를 의 에 에서 에게 께 로 으로 와 과 도 만 랑 이랑 하고 부터 까지 처럼 보다 "
    "이나 나 든 이든 라도 이라도 이란 란 이라는 라는 이다 입니다 해줘 해 줘".split(),
    key=len,
    reverse=True,
)
STOPWORDS = set(
    "알려줘 알려 줘 해줘 해주세요 주세요 좀 그리고 지금 뭐 뭐야 어떻게 무엇 무슨 어디 있어 있나 하는 할 "
    "그 이 저 것 거 수 등 및 더 the a an of to and or for in on at is are be was what how why "
    "please me my show tell about with this that it".split()
)
TASK_WORDS = ("할 일", "할일", "todo", "to-do", "task", "남은", "다음 작업", "다음에", "해야")
STATUS_WORDS = ("상태", "진행", "어디까지", "현황", "status", "progress", "이어서", "이어 ", "계속")


def strip_particle(word: str) -> str:
    for particle in _PARTICLES:
        if len(word) > len(particle) + 1 and word.endswith(particle):
            return word[: -len(particle)]
    return word


def features(text: str, keep_stopwords: bool = False) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text.casefold()):
        if "가" <= word[0] <= "힣":
            stem = strip_particle(word)
            if not keep_stopwords and (stem in STOPWORDS or word in STOPWORDS):
                continue
            # keep both forms: "재시도" must not become only "재시" because 도 is also a particle
            for form in dict.fromkeys((word, stem)):
                if len(form) >= 2:
                    out.append(form)
            if len(word) >= 3:
                out.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            if (not keep_stopwords and word in STOPWORDS) or len(word) < 2:
                continue
            out.append(word)
            if "-" in word or "." in word or "_" in word:
                out.extend(p for p in re.split(r"[-._]", word) if len(p) >= 2)
    return out


def jaccard(a: str, b: str) -> float:
    fa, fb = set(features(a)), set(features(b))
    return len(fa & fb) / len(fa | fb) if fa and fb else 0.0


def has_any(text: str, words: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(word in folded for word in words)


def estimate_tokens(text: str) -> int:
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    return hangul + (len(text) - hangul + 3) // 4
