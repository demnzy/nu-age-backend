import sys
sys.path.insert(0, ".")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from nu_voice.linguistics.normalizer import (
    normalize_hausa_orthography,
    expand_numbers_and_symbols,
    clean_markdown_for_speech,
    split_into_sentences,
    prepare_text_for_speech,
)

print("--- Test 1: Hausa Hooked Letters ---")
hausa_sample = "'Dalibi ya je 'kasa mai 'yanci don koyon 'baki da 'barna."
norm_hausa = normalize_hausa_orthography(hausa_sample)
print("Original:", hausa_sample)
print("Normalized:", norm_hausa)
assert "Ɗalibi" in norm_hausa
assert "ƙasa" in norm_hausa
assert "ƴanci" in norm_hausa
assert "ɓaki" in norm_hausa

print("--- Test 2: Hausa Symbols and Numbers ---")
num_sample = "Muna da dalibai 50% da ₦500."
norm_nums = expand_numbers_and_symbols(num_sample, lang="ha")
print("Original:", num_sample)
print("Expanded:", norm_nums)
assert "kashi hamsin cikin ɗari" in norm_nums
assert "naira ɗari biyar" in norm_nums

print("--- Test 3: Markdown Stripping ---")
md_sample = "# Introduction\nIn this lesson, visit [Nu-Age](https://nu-age.com) and run:\n```python\nprint('hello')\n```\nIt costs $10!"
clean_md = clean_markdown_for_speech(md_sample)
print("Cleaned MD:", clean_md)
assert "Introduction" in clean_md
assert "https://" not in clean_md
assert "Nu-Age" in clean_md

print("--- Test 4: Sentence Chunking ---")
chunk_sample = "Dr. Bello visited Prof. Smith at 3.14 PM. They discussed lesson 1. Then they left!"
chunks = split_into_sentences(chunk_sample)
print("Chunks:", chunks)
assert len(chunks) == 3
assert "Dr. Bello" in chunks[0]

print("ALL NORMALIZER TESTS PASSED PERFECTLY!")
