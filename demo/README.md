# Conversion corpus

Every conversion delivered or used for development is kept here: the input PDF, the accepted EPUB, and a
`.quality.json` baseline. `tests/test_corpus.py` and `tools/corpus_gate.py` re-convert each PDF with the
current code and fail if any quality metric gets worse. Code changes are committed only when the gate passes,
and improvements are accepted with `python tools/corpus_gate.py --update`, which rewrites these files.

To read one on Kindle, download the `.epub` and use Send to Kindle (Android: Share → Kindle, or email it to
your `@kindle.com` address). The Kindle app does not open `.epub` files tapped in a file manager.

Columns: fig/tab/eq = figures, tables, display equations; recall = share of the PDF's words present in the
EPUB (text and alt text); order = reading-order score; issues = lint findings (lower is better).

| Files | Title | Pages | fig/tab/eq | recall | order | issues |
| --- | --- | ---: | :---: | ---: | ---: | ---: |
| [1412.6980](1412.6980.pdf) / [epub](1412.6980.epub) | Adam: A Method for Stochastic Optimization | 15 | 4/0/30 | 0.896 | 0.981 | 32 |
| [1512.03385](1512.03385.pdf) / [epub](1512.03385.epub) | Deep Residual Learning for Image Recognition | 12 | 7/17/2 | 0.842 | 0.912 | 5 |
| [1706.03762](1706.03762.pdf) / [epub](1706.03762.epub) | Attention Is All You Need | 15 | 5/4/5 | 0.894 | 0.971 | 3 |
| [1810.04805](1810.04805.pdf) / [epub](1810.04805.epub) | BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding | 16 | 5/9/0 | 0.898 | 0.950 | 18 |
| [2002.05709](2002.05709.pdf) / [epub](2002.05709.epub) | A Simple Framework for Contrastive Learning of Visual Representations | 20 | 26/16/4 | 0.825 | 0.940 | 48 |
| [2406.11717v3](2406.11717v3.pdf) / [epub](2406.11717v3.epub) |  |  | 10/12/11 | 0.913 | 0.990 | 15 |
| [2505.19056](2505.19056.pdf) / [epub](2505.19056.epub) | An Embarrassingly Simple Defense Against LLM Abliteration Attacks | 18 | 14/5/3 | 0.874 | 0.992 | 16 |
| [2610.08937](2610.08937.pdf) / [epub](2610.08937.epub) | A Shortcut to Structure in AlphaFold 3 | 43 | 8/17/39 | 0.947 | 0.986 | 25 |
| [2610.10482](2610.10482.pdf) / [epub](2610.10482.epub) | Truecell reproduces R Seurat's single-cell analysis outputs natively in the Python ecosystem | 24 | 9/4/0 | 0.870 | 1.000 | 12 |
| [2610.10913](2610.10913.pdf) / [epub](2610.10913.epub) | Toward Reliable Patient-Specific Aortic Strain Mapping from 4D CTA: Validation, Spectral Structure, and Clinical Potential | 30 | 8/6/13 | 0.974 | 0.993 | 21 |
| [2610.11729](2610.11729.pdf) / [epub](2610.11729.epub) | arXiv | 36 | 17/4/0 | 0.960 | 0.987 | 20 |
| [2610.12426](2610.12426.pdf) / [epub](2610.12426.epub) | Lyapunov spectrum of random neural networks | 36 | 4/1/110 | 0.975 | 0.996 | 68 |
