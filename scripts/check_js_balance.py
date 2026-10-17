import sys
import re

path = sys.argv[1]
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

# extract script contents
scripts = re.findall(r'<script[^>]*>([\s\S]*?)</script>', text, flags=re.IGNORECASE)
if not scripts:
    print('No <script> content found')
    sys.exit(0)

pairs = {'(':')','{':'}','[':']'}

def check_js(src):
    open_stack = []
    i = 0
    L = len(src)
    state = 'code'
    string_char = ''
    while i < L:
        ch = src[i]
        nxt = src[i+1] if i+1 < L else ''
        if state == 'code':
            if ch == '/' and nxt == '/':
                state = 'line_comment'
                i += 1
            elif ch == '/' and nxt == '*':
                state = 'block_comment'
                i += 1
            elif ch == '"' or ch == "'" or ch == '`':
                string_char = ch
                state = 'string'
            elif ch in pairs:
                open_stack.append((ch, i))
            elif ch in pairs.values():
                if not open_stack:
                    ln = src.count('\n', 0, i) + 1
                    col = i - src.rfind('\n', 0, i)
                    return ('extra_closing', ch, i, ln, col)
                last, pos = open_stack.pop()
                if pairs[last] != ch:
                    ln1 = src.count('\n', 0, pos) + 1
                    col1 = pos - src.rfind('\n', 0, pos)
                    ln2 = src.count('\n', 0, i) + 1
                    col2 = i - src.rfind('\n', 0, i)
                    start1 = max(0, pos-80)
                    end1 = min(len(src), pos+80)
                    start2 = max(0, i-80)
                    end2 = min(len(src), i+80)
                    ctx1 = src[start1:end1].replace('\n','\\n')
                    ctx2 = src[start2:end2].replace('\n','\\n')
                    return ('mismatch', last, pos, ln1, col1, ch, i, ln2, col2, ctx1, ctx2)
        elif state == 'line_comment':
            if ch == '\n':
                state = 'code'
        elif state == 'block_comment':
            if ch == '*' and nxt == '/':
                state = 'code'
                i += 1
        elif state == 'string':
            if ch == '\\':
                i += 1  # skip escaped char
            elif ch == string_char:
                state = 'code'
        i += 1
    if open_stack:
        return ('unclosed', open_stack)
    return ('ok',)

for idx, s in enumerate(scripts, start=1):
    res = check_js(s)
    if res[0] != 'ok':
        print(f'Script #{idx} has problem: {res[0]}')
        if res[0] == 'extra_closing':
            _, ch, i, ln, col = res
            print(f'Extra closing {ch} at index {i} (line {ln}, col {col})')
        elif res[0] == 'mismatch':
            _, last, pos, ln1, col1, ch, i, ln2, col2, ctx1, ctx2 = res
            print(f'Mismatched {last} at index {pos} (line {ln1}, col {col1}) with {ch} at index {i} (line {ln2}, col {col2})')
            print('Context around opening:', ctx1)
            print('Context around closing:', ctx2)
            # map back to HTML
            idx1 = text.find(ctx1[:40].replace('\\n','\n'))
            if idx1 != -1:
                ln = text.count('\n', 0, idx1) + 1
                col = idx1 - text.rfind('\n', 0, idx1)
                print(f'Opening context appears in HTML at line {ln}, col {col}')
            idx2 = text.find(ctx2[:40].replace('\\n','\n'))
            if idx2 != -1:
                ln = text.count('\n', 0, idx2) + 1
                col = idx2 - text.rfind('\n', 0, idx2)
                print(f'Closing context appears in HTML at line {ln}, col {col}')
        elif res[0] == 'unclosed':
            for ch,pos in res[1]:
                ln = s.count('\n', 0, pos) + 1
                col = pos - s.rfind('\n', 0, pos)
                print(f'Unclosed {ch} at index {pos} (line {ln}, col {col})')
        sys.exit(1)
print('All scripts individually balanced')
sys.exit(0)
