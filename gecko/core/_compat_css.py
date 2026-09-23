"""给 CSS 里所有 var(--x) 补一行「写死的具体值」，作为老内核（IE11 / 老 WebView）兜底。

原理：老内核不认识 CSS 变量，会把「含 var() 的那一条声明」整条丢掉；
      在它前面补一行同属性、值写死的普通声明，老内核就能吃到。
      现代浏览器按「后写覆盖前写」仍然采用 var() 的值（深色模式照旧），外观分毫不差。

用法：  python _compat_css.py [文件...]
       - .css  整个文件都处理
       - .html 只处理 <style> ... </style> 块内部，绝不动 style="" 属性和 JS
      可重复执行：先清掉带 /* fb */ 标记的旧兜底行，再整体重新插入，结果稳定。
"""
import re
import sys

# 声明： prop: ...var(--x)...;  允许跨行，值里不得出现 ; { }
DECL = re.compile(r"([ \t]*)([a-zA-Z-]+)(\s*:\s*)([^;{}]*var\([^;{}]*?)\s*;")
VARDEF = re.compile(r"(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);")
VARUSE = re.compile(r"var\(\s*(--[a-zA-Z0-9-]+)\s*(?:,\s*([^()]*?)\s*)?\)")
STYLE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.S | re.I)

MARK = "/* fb */"          # 兜底行标记，用来做幂等清除
MARK_RE = re.compile(r"^[ \t]*[a-zA-Z-]+[ \t]*:[^;]*;[ \t]*" + re.escape(MARK) + r"[ \t]*$")


def load_vars(text, into):
    """抽取 :root 块里定义的变量（浅色那一份优先，后读的不覆盖已有键）。"""
    for m in re.finditer(r":root\s*\{([^}]*)\}", text):
        for name, val in VARDEF.findall(m.group(1)):
            into.setdefault(name, val.strip())
    return into


def resolve(value, table):
    """把 var(--x) 递归替换成具体值；有变量查不到就返回 None（跳过该声明）。"""
    out = value
    for _ in range(6):
        if "var(" not in out:
            break
        missing = False

        def sub(m):
            nonlocal missing
            name, fb = m.group(1), m.group(2)
            if name in table:
                return table[name]
            if fb:
                return fb
            missing = True
            return m.group(0)

        out = VARUSE.sub(sub, out)
        if missing:
            return None
    return None if "var(" in out else out


def dedupe_decls(line):
    """同一行里重复声明的属性只留最后一条。

    换主题后重跑本脚本，会在旧兜底行旁边再插一条，久了会攒成
    「旧浅色 ; 新深色 ; var()」三层 —— 浏览器取值没错，但源码里留着上代色值
    会误导后续维护。这里先压平，保证重复执行结果是稳定的单层兜底。
    """
    hits = [(m.group(1), m.start(), m.end()) for m in DECL.finditer(line)]
    if not hits:
        return line
    props = [h[0] for h in hits]
    out, cut = [], 0
    for i, (prop, start, end) in enumerate(hits):
        if prop in props[i + 1:]:      # 后面还会再声明一次 → 当前这条是历史残留
            out.append(line[cut:start])
            cut = end
    out.append(line[cut:])
    return "".join(out)


def css_regions(text, is_html):
    """返回允许写入的区间 [(start, end)]。HTML 只在 <style> 块内动手。"""
    if not is_html:
        return [(0, len(text))]
    return [(m.start(1), m.end(1)) for m in STYLE.finditer(text)]


def patch(path, table):
    text = open(path, encoding="utf-8").read()
    lines = text.split("\n")
    eol = "\r\n" if text.count("\r\n") * 2 > text.count("\n") else "\n"

    # 1. 清掉上一轮插入的兜底行（保证重复执行结果一致）
    kept = [ln for ln in lines if not MARK_RE.match(ln)]
    removed = len(lines) - len(kept)
    text = "\n".join(kept)

    # 1.5 压平残留的重复声明（HTML 只在 <style> 区域内动手，绝不碰 JS 与属性）
    regions0 = css_regions(text, path.lower().endswith(".html"))
    flat, pos = [], 0
    for ln in kept:
        start = pos
        pos += len(ln) + 1
        flat.append(dedupe_decls(ln) if any(a <= start < b for a, b in regions0) else ln)
    text = "\n".join(flat)

    # 2. 只在允许的区间里插入
    regions = css_regions(text, path.lower().endswith(".html"))
    hits = skipped = 0
    out, pos = [], 0
    for m in DECL.finditer(text):
        if not any(a <= m.start() < b for a, b in regions):
            skipped += 1
            continue
        indent, prop, sep, value = m.groups()
        literal = resolve(value, table)
        if literal is None:
            skipped += 1
            continue
        # 跨行声明压成一行（CSS 语义不变，且方便下次清除）
        flat = re.sub(r"\s+", " ", literal).strip()

        # 判断这条声明是不是「独占一行」：是→上方插一行；否（写在 { ... } 同一行）→就地插
        ls = text.rfind("\n", 0, m.start()) + 1          # 本行起点
        le = text.find("\n", m.end())                     # 本行结尾（-1 表示文件末尾）
        le = len(text) if le < 0 else le
        alone = (text[ls : m.start()].strip() == "" and text[m.end() : le].strip() == "")

        out.append(text[pos : m.start()])
        if alone:
            out.append("%s%s%s%s; %s%s" % (indent, prop, sep, flat, MARK, eol))
        else:
            # 行内插：前面已经有同样的兜底就不重复写（幂等）
            tail = re.sub(r"\s+", "", text[: m.start()])[-160:]
            if not tail.endswith(re.sub(r"\s+", "", prop + ":" + flat + ";")):
                out.append("%s%s:%s; " % (indent, prop, flat))
        pos = m.start()
        hits += 1
    out.append(text[pos:])
    open(path, "w", encoding="utf-8", newline="").write("".join(out))
    return hits, skipped, removed


if __name__ == "__main__":
    # 先读 apple.css 的浅色变量：它是最终生效的主题，兜底值以它为准
    table = load_vars(open("apple.css", encoding="utf-8").read(), {})
    # 再补 webui.html 自己定义、apple.css 没有的变量
    table = load_vars(open("webui.html", encoding="utf-8").read(), table)

    for f in sys.argv[1:] or ["apple.css", "webui.html"]:
        h, s, rm = patch(f, table)
        print("%-11s 插入兜底 %3d 条（清除旧兜底 %3d 条），未处理 %d 条"
              % (f, h, rm, s))
