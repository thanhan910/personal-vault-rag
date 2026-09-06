from pathlib import Path

from app.extraction import extract_document


def test_markdown_keeps_heading_and_no_fabricated_page(tmp_path: Path):
    path = tmp_path / "note.md"
    path.write_text("# Vietnamese\n\nXin chào thế giới.\n\n## Identifier\n\nExact code ZX-91-ALPHA.", encoding="utf-8")
    result = extract_document(path, path.name, tmp_path / "previews")
    text = " ".join(chunk.text for chunk in result.chunks)
    assert "Xin chào" in text and "ZX-91-ALPHA" in text
    assert all("page" not in chunk.locator for chunk in result.chunks)
