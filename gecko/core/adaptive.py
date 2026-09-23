#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adaptive.py —— 自愈选择器（v1.5.0）

灵感来自 Scrapling 的自适应匹配（AutoMatch）：网站改版后，原先配置的
CSS 选择器往往一个都匹配不上，数据直接断流。本模块给每个选择器维护一份
「成功时的元素指纹」，一旦选择器失效，就按指纹相似度在全页面里重新定位
最像的那个元素——无需人工改配置。

指纹维度（各带权重）：
  - 标签名            改版一般不会换标签
  - class 集合        改版常改类名，用 Jaccard 相似度容忍部分改动
  - id                用 difflib 序列相似度，容忍 "productCard" -> "product-card"
  - 关键属性          href/src/data-* 等，值变化但有迹可循
  - 文本内容          前 80 字做序列相似度
  - 父链              结构上下文（tag 链），整体位移不敏感

只依赖 BeautifulSoup + 标准库，没有额外安装成本。
"""

from difflib import SequenceMatcher
import re

# 相似度门槛：低于它的候选一律不认，宁可空手也不乱抓
THRESHOLD = 3.0

_W_TAG = 2.0
_W_CLASS = 3.0
_W_ID = 2.0
_W_ATTR = 1.5
_W_TEXT = 1.0       # 列表页里每条的文本都不同，文本只作辅助，不能压过结构
_W_PARENT = 1.0

_INTERESTING_ATTRS = ("href", "src", "data-src", "action", "value", "title", "name", "type", "rel")

_text_cache = {}

def _norm_text(node):
    """取元素压缩后的前 80 个字符，供文本相似度用。"""
    key = id(node)
    v = _text_cache.get(key)
    if v is None:
        try:
            v = re.sub(r"\s+", " ", node.get_text(" ", strip=True))[:80]
        except Exception:
            v = ""
        _text_cache[key] = v
    return v


def fingerprint(node, parent_depth=3):
    """提取元素指纹（dict）。parent_depth 控制父链长度。"""
    if node is None:
        return None
    chain = []
    p = node.parent
    for _ in range(parent_depth):
        if p is None or getattr(p, "name", None) in (None, "[document]", "html", "body"):
            break
        chain.append(p.name)
        p = p.parent
    classes = []
    try:
        classes = list(node.get("class") or [])
    except Exception:
        pass
    attrs = {}
    try:
        for k in _INTERESTING_ATTRS:
            v = node.get(k)
            if v:
                attrs[k] = str(v)[:120]
    except Exception:
        pass
    return {
        "tag": getattr(node, "name", ""),
        "id": node.get("id") or "",
        "classes": classes,
        "attrs": attrs,
        "text": _norm_text(node),
        "parents": chain,
    }


def _jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 0.0
    inter = a & b
    return len(inter) / (len(a) + len(b) - len(inter)) if (a or b) else 0.0


def _ratio(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def similarity(fp, cand_fp):
    """两个指纹的加权相似度得分（0 ~ 10 左右）。"""
    if not fp or not cand_fp:
        return 0.0
    score = 0.0
    if fp["tag"] == cand_fp["tag"]:
        score += _W_TAG
    score += _W_CLASS * _jaccard(fp["classes"], cand_fp["classes"])
    score += _W_ID * _ratio(fp["id"], cand_fp["id"])
    same_attrs = 0
    for k, v in fp["attrs"].items():
        v2 = cand_fp["attrs"].get(k)
        if v2 and _ratio(v, v2) > 0.55:
            same_attrs += 1
    if fp["attrs"]:
        score += _W_ATTR * same_attrs
    score += _W_TEXT * _ratio(fp["text"], cand_fp["text"])
    score += _W_PARENT * _jaccard(fp["parents"], cand_fp["parents"])
    return score


class AdaptiveSelector:
    """按选择器维度学习/自愈。

    用法：
        adp = AdaptiveSelector()
        nodes = soup.select(spec)
        if not nodes and adp.enabled:
            nodes, healed = adp.heal(soup, spec)
            if healed: adp.learn(spec, nodes[0])
        elif nodes:
            adp.learn(spec, nodes[0])
    """

    def __init__(self, enabled=True, threshold=THRESHOLD, max_cache=256):
        self.enabled = bool(enabled)
        self.threshold = float(threshold)
        self._profiles = {}      # spec -> 指纹
        self._order = []         # LRU 淘汰
        self.max_cache = int(max_cache)
        self.heal_count = 0      # 统计：本任务自愈命中次数
        self.miss_count = 0      # 统计：自愈也没救回来的次数

    # ---- 学习 ----
    def learn(self, spec, node):
        if not self.enabled or node is None:
            return
        fp = fingerprint(node)
        if not fp:
            return
        if spec not in self._profiles and len(self._profiles) >= self.max_cache:
            old = self._order.pop(0)
            self._profiles.pop(old, None)
        self._profiles[spec] = fp
        if spec in self._order:
            self._order.remove(spec)
        self._order.append(spec)

    # ---- 自愈 ----
    def heal(self, soup, spec, scope=None, strict=False):
        """选择器失配时按指纹重新定位。

        scope：限定搜索范围的容器（逐条模式传容器节点），None 则全页。
        strict=True 用于「记录容器」这类关键选择器：只接受与最优候选高度接近的元素
        （相对门槛 0.85），避免把页面里偶然长得像的装饰元素当成列表救回来。
        返回 (nodes, healed)；healed=False 表示无匹配也无自愈。
        """
        if not self.enabled:
            return [], False
        fp = self._profiles.get(spec)
        if not fp:
            self.miss_count += 1
            return [], False

        root = scope if scope is not None else soup
        # 候选集：优先同名标签，其次同 class，再退化为全元素（大页面会慢，设上限）
        cands = []
        try:
            if fp["tag"]:
                cands = root.find_all(fp["tag"], limit=400)
            if not cands and fp["classes"]:
                cands = root.find_all(class_=fp["classes"][0], limit=400)
            if not cands:
                cands = root.find_all(True, limit=800)
        except Exception:
            return [], False

        scored = []
        for c in cands:
            s = similarity(fp, fingerprint(c))
            if s >= self.threshold:
                scored.append((s, c))
        if not scored:
            self.miss_count += 1
            return [], False
        scored.sort(key=lambda x: -x[0])
        top_score = scored[0][0]
        # 过滤用绝对阈值（>= threshold）为主、相对最优候选为辅：
        # 列表页里每条文本都不同，若按「最接近第一条」过滤就只剩第一条能救回来，
        # 那正是自愈最该覆盖的场景。0.6 的相对门槛只用来甩掉明显不相关的元素。
        rel = 0.85 if strict else 0.6
        picked = [c for s, c in scored
                  if s >= self.threshold and s >= top_score * rel][:30]
        self.heal_count += 1
        return picked, True
