# Conversion corpus

Every conversion delivered or used for development is kept here, in `PDF/` (papers) and `web/` (web
articles): the source, the accepted EPUB, and a `.quality.json` baseline. `tests/test_corpus.py` and
`tools/corpus_gate.py` re-convert each source with the
current code and fail if any quality metric gets worse. Code changes are committed only when the gate passes,
and improvements are accepted with `python tools/corpus_gate.py --update`, which rewrites these files.
Files are named `{title_in_snake_case}-{arxiv_id}.{pdf,epub,quality.json}`.

To read one on Kindle, download the `.epub` and use Send to Kindle (Android: Share → Kindle, or email it to
your `@kindle.com` address). The Kindle app does not open `.epub` files tapped in a file manager.

Columns: fig/tab/eq = figures, tables, display equations; recall = share of the PDF's words present in the
EPUB (text and alt text); order = reading-order score; issues = lint findings (lower is better).

| arXiv | Title | Files | Pages | fig/tab/eq | recall | order | issues |
| --- | --- | --- | ---: | :---: | ---: | ---: | ---: |
| [2610.08937](https://arxiv.org/abs/2610.08937) | A Shortcut to Structure in AlphaFold 3 | [pdf](PDF/a_shortcut_to_structure_in_alphafold_3-2610.08937.pdf) · [epub](PDF/a_shortcut_to_structure_in_alphafold_3-2610.08937.epub) | 43 | 8/17/39 | 0.948 | 0.988 | 15 |
| [2002.05709](https://arxiv.org/abs/2002.05709) | A Simple Framework for Contrastive Learning of Visual Representations | [pdf](PDF/a_simple_framework_for_contrastive_learning_of_visual_representations-2002.05709.pdf) · [epub](PDF/a_simple_framework_for_contrastive_learning_of_visual_representations-2002.05709.epub) | 20 | 26/16/6 | 0.826 | 0.940 | 41 |
| [1412.6980](https://arxiv.org/abs/1412.6980) | Adam: A Method for Stochastic Optimization | [pdf](PDF/adam_a_method_for_stochastic_optimization-1412.6980.pdf) · [epub](PDF/adam_a_method_for_stochastic_optimization-1412.6980.epub) | 15 | 4/0/27 | 0.901 | 0.981 | 20 |
| [2505.19056](https://arxiv.org/abs/2505.19056) | An Embarrassingly Simple Defense Against LLM Abliteration Attacks | [pdf](PDF/an_embarrassingly_simple_defense_against_llm_abliteration_attacks-2505.19056.pdf) · [epub](PDF/an_embarrassingly_simple_defense_against_llm_abliteration_attacks-2505.19056.epub) | 18 | 7/5/6 | 0.876 | 1.000 | 5 |
| [1706.03762](https://arxiv.org/abs/1706.03762) | Attention Is All You Need | [pdf](PDF/attention_is_all_you_need-1706.03762.pdf) · [epub](PDF/attention_is_all_you_need-1706.03762.epub) | 15 | 5/4/5 | 0.894 | 0.971 | 3 |
| [1810.04805](https://arxiv.org/abs/1810.04805) | BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding | [pdf](PDF/bert_pre_training_of_deep_bidirectional_transformers_for_language_understanding-1810.04805.pdf) · [epub](PDF/bert_pre_training_of_deep_bidirectional_transformers_for_language_understanding-1810.04805.epub) | 16 | 5/8/0 | 0.906 | 0.956 | 9 |
| [1512.03385](https://arxiv.org/abs/1512.03385) | Deep Residual Learning for Image Recognition | [pdf](PDF/deep_residual_learning_for_image_recognition-1512.03385.pdf) · [epub](PDF/deep_residual_learning_for_image_recognition-1512.03385.epub) | 12 | 7/17/2 | 0.844 | 0.918 | 1 |
| [2610.12426](https://arxiv.org/abs/2610.12426) | Lyapunov spectrum of random neural networks | [pdf](PDF/lyapunov_spectrum_of_random_neural_networks-2610.12426.pdf) · [epub](PDF/lyapunov_spectrum_of_random_neural_networks-2610.12426.epub) | 36 | 4/1/110 | 0.975 | 0.996 | 39 |
| [2406.11717v3](https://arxiv.org/abs/2406.11717v3) | Refusal in Language Models Is Mediated by a Single Direction | [pdf](PDF/refusal_in_language_models_is_mediated_by_a_single_direction-2406.11717v3.pdf) · [epub](PDF/refusal_in_language_models_is_mediated_by_a_single_direction-2406.11717v3.epub) | 40 | 10/12/11 | 0.914 | 0.990 | 1 |
| [2610.11729](https://arxiv.org/abs/2610.11729) | Three-dimensional imaging of isolated membrane-protein complexes in vacuo with an X-ray laser | [pdf](PDF/three_dimensional_imaging_of_isolated_membrane_protein_complexes_in_vacuo_with_an_x_ray_laser-2610.11729.pdf) · [epub](PDF/three_dimensional_imaging_of_isolated_membrane_protein_complexes_in_vacuo_with_an_x_ray_laser-2610.11729.epub) | 36 | 17/4/0 | 0.972 | 0.994 | 18 |
| [2610.10913](https://arxiv.org/abs/2610.10913) | Toward Reliable Patient-Specific Aortic Strain Mapping from 4D CTA: Validation, Spectral Structure, and Clinical Potential | [pdf](PDF/toward_reliable_patient_specific_aortic_strain_mapping_from_4d_cta_validation_spectral_structure_and_clinical_potential-2610.10913.pdf) · [epub](PDF/toward_reliable_patient_specific_aortic_strain_mapping_from_4d_cta_validation_spectral_structure_and_clinical_potential-2610.10913.epub) | 30 | 8/6/13 | 0.975 | 0.993 | 14 |
| [2610.10482](https://arxiv.org/abs/2610.10482) | Truecell reproduces R Seurat's single-cell analysis outputs natively in the Python ecosystem | [pdf](PDF/truecell_reproduces_r_seurats_single_cell_analysis_outputs_natively_in_the_python_ecosystem-2610.10482.pdf) · [epub](PDF/truecell_reproduces_r_seurats_single_cell_analysis_outputs_natively_in_the_python_ecosystem-2610.10482.epub) | 24 | 9/4/0 | 0.870 | 1.000 | 11 |

## Web articles (`pdf2epub web`)

The web converter's corpus is in [`web/`](web/): the accepted EPUB and a baseline with the page URLs. The
page snapshots stay in the git-ignored cache `.corpus/web/`; the gate re-snapshots a page when that cache
is empty. recall is measured against the page's visible text.

| File | Title | Source | fig/tab/eq | recall | order | issues |
| --- | --- | --- | :---: | ---: | ---: | ---: |
| [epub](web/dust_pretraining_transformers_without_backpropagation.epub) | Dust: Pretraining Transformers Without Backpropagation | [page 1](https://qlabs.sh/research/dust) · [page 2](https://qlabs.sh/research/dust/appendix.html) | 7/8/5 | 0.966 | 1.000 | 0 |
