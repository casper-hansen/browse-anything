"""HTML to markdown for `browse`, keeping the links a page's text alone would lose."""

from markdownify import MarkdownConverter


class Converter(MarkdownConverter):
    def convert_a(self, el, text, parent_tags):
        # Listing cards are often covered by a link with no text, only an aria-label ("Voir l'annonce"). markdownify
        # drops such a link, and with it the only URL of the listing.
        if not text.strip() and el.get("href") and not el.get("href", "").startswith(("javascript:", "#")):
            text = (el.get("aria-label") or el.get("title") or "").strip() or "link"
        return super().convert_a(el, text, parent_tags)


def to_markdown(html: str) -> str:
    return Converter(heading_style="ATX").convert(html)
