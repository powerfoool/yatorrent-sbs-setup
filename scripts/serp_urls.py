"""Fail-closed, offline URL extraction for saved Yandex organic SERPs."""
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin


class SerpParseError(ValueError):
    pass


def validate_url(value):
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value:
        raise SerpParseError('SERP URL must be an absolute HTTP(S) URL')
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
    except ValueError:
        raise SerpParseError('SERP URL must be an absolute HTTP(S) URL') from None
    return value


def validate_top5(value):
    if not isinstance(value, list) or len(value) > 5:
        raise SerpParseError('SERP ranking must contain zero to five organic URLs')
    result = []
    for position, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) != {'position', 'url'} or type(item['position']) is not int or item['position'] != position:
            raise SerpParseError('SERP ranking requires ordered position/url objects numbered contiguously from 1')
        result.append({'position': position, 'url': validate_url(item['url'])})
    return result


class Node:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def has(self, name):
        return name in self.attrs.get('class', '').split()

    def closest(self, name):
        node = self
        while node:
            if node.has(name):
                return node
            node = node.parent
        return None

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class Parser(HTMLParser):
    void = set('area base br col embed hr img input link meta param source track wbr'.split())

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.current = self.root
        self.bad = False
        self.html_closed = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        # Duplicate structural attributes cannot select one of two meanings.
        if len({k for k, _ in attrs}) != len(attrs):
            self.bad = True
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.void:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.void:
            return
        node = self.current
        while node.parent:
            if node.tag == tag:
                # HTML optional end tags are allowed, but organic structural
                # containers and links must be closed explicitly.
                cursor = self.current
                while cursor is not node:
                    if cursor.tag == 'a' or cursor.has('Organic') or cursor.has('serp-item'):
                        self.bad = True
                    cursor = cursor.parent
                self.current = node.parent
                if tag == 'html':
                    self.html_closed = True
                return
            node = node.parent
        self.bad = True


def extract_top5(html, source_url=None):
    if not isinstance(html, str) or not html.strip():
        raise SerpParseError('Saved SERP HTML required for local organic URL extraction')
    try:
        parser = Parser(html)
    except (ValueError, AssertionError):
        raise SerpParseError('Malformed saved SERP HTML; supply verified URL rankings') from None
    if parser.bad or not parser.html_closed or parser.current is not parser.root:
        raise SerpParseError('Malformed saved SERP HTML; supply verified URL rankings')
    if source_url is not None:
        validate_url(source_url)
    urls, cards = [], set()
    for anchor in parser.root.walk():
        if anchor.tag != 'a' or not anchor.has('OrganicTitle-Link'):
            continue
        organic, card = anchor.closest('Organic'), anchor.closest('serp-item')
        if organic is None and anchor.closest('VideoSnippet') is not None:
            continue  # Observed video special block reuses the title class.
        if organic is None or card is None:
            raise SerpParseError('Ambiguous organic result structure; supply verified URL rankings')
        href = anchor.attrs.get('href', '')
        try:
            host = urlsplit(href).hostname
        except ValueError:
            raise SerpParseError('Malformed organic URL') from None
        ancestors = []
        node = anchor
        while node:
            ancestors.append(node)
            node = node.parent
        if host and host.rstrip('.') == 'yabs.yandex.ru' or any(n.has('serp-item_type_ad') or n.has('Organic_type_ad') for n in ancestors):
            continue
        if not urlsplit(href).scheme:
            if source_url is None:
                raise SerpParseError('Relative organic URL requires local source URL metadata')
            href = urljoin(source_url, href)
        validate_url(href)
        if card in cards:
            raise SerpParseError('Ambiguous multiple organic title URLs in one result card')
        cards.add(card)
        urls.append(href)
    empty = any(node.has('EmptySearchResults') for node in parser.root.walk())
    if empty and urls:
        raise SerpParseError('Ambiguous SERP has both empty-search and organic-result markers')
    if not urls and not empty:
        raise SerpParseError('Unknown SERP structure: no organic results or recognized empty-search marker')
    return validate_top5([{'position': i, 'url': url} for i, url in enumerate(urls[:5], 1)])
