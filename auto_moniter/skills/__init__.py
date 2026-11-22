"""Load the built-in skill guidance included in the distribution."""

from functools import lru_cache
from importlib.resources import files
import re


@lru_cache(maxsize=None)
def guidance(name: str) -> str:
    skill = files(__package__).joinpath(name, 'SKILL.md').read_text(encoding='utf-8')
    return skill.partition('\n---\n')[2].strip()


def browser_guidance(text: str) -> str:
    if re.search(r'\b(?:browser|playwright|chromium|viewport|selenium|puppeteer|cypress|webdriver)\b'
                 r'|浏览器|视口|断点|拖拽', text, re.IGNORECASE):
        return guidance('browser-acceptance')
    return ''
