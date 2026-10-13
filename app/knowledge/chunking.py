"""Paragraph splitting, with repeated ATX heading context for Markdown."""
import re
from langchain_text_splitters import RecursiveCharacterTextSplitter


def split_text(text, config, source_format='text'):
    def paragraphs(body, prefix=''):
        size = config.chunk_size - len(prefix)
        splitter = RecursiveCharacterTextSplitter(chunk_size=size,
            chunk_overlap=min(config.chunk_overlap, size - 1),
            separators=['\n\n', '\n', '。', '！', '？', '；', '，', ' ', ''])
        return [prefix + part for part in splitter.split_text(body) if part.strip()]

    if source_format != 'markdown':
        return paragraphs(text)
    chunks, headings, lines, fence = [], [], [], None

    def flush():
        # Bound long heading context so the configured chunk limit still holds.
        prefix = '\n'.join(value for _, value in headings)
        prefix = prefix[:config.chunk_size // 2] + '\n\n' if prefix else ''
        body = '\n'.join(lines).strip()
        chunks.extend(paragraphs(body, prefix) if body else ([prefix.strip()] if prefix else []))
        lines.clear()

    for line in text.splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if marker:
            symbol, rest = marker.groups()
            if fence is None:
                fence = symbol
            elif symbol[0] == fence[0] and len(symbol) >= len(fence) and not rest.strip():
                fence = None
            lines.append(line)
            continue
        header = re.match(r'^ {0,3}(#{1,6})\s+(.+?)\s*$', line) if fence is None else None
        if header:
            flush()
            level = len(header[1])
            headings[:] = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, header[1] + ' ' + re.sub(r'\s+#+\s*$', '', header[2])))
        else:
            lines.append(line)
    flush()
    return chunks
