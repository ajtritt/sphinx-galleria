import sphinx_galleria  # noqa
from sphinx_galleria.sorting import FileNameSortKey

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.autosummary',
    'sphinx.ext.intersphinx',
    'sphinx_galleria.gen_gallery',
]
templates_path = ['_templates']
autosummary_generate = True
source_suffix = '.rst'
master_doc = 'index'
exclude_patterns = ['_build']
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'numpy': ('https://docs.scipy.org/doc/numpy/', None),
    'scipy': ('https://docs.scipy.org/doc/scipy/reference', None),
    'matplotlib': ('https://matplotlib.org/', None),
}
sphinx_galleria_conf = {
    'doc_module': ('sphinx_galleria',),
    'reference_url': {
        'sphinx_galleria': None,
        },
    'examples_dirs': ['examples'],
    'gallery_dirs': ['auto_examples'],
    'backreferences_dir': 'gen_modules/backreferences',
    'within_section_order': FileNameSortKey,
    'expected_failing_examples': ['examples/plot_future_imports_broken.py'],
}
nitpicky = True
