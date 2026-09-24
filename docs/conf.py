"""Sphinx configuration for GraFlag documentation."""

import os
import sys

# The repository root, so autodoc imports the graflag package next to docs/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

project = 'GraFlag'
author = 'gbay7'
version = '1.2'
release = '1.2.0'

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx.ext.intersphinx',
    'sphinx.ext.githubpages',   # writes .nojekyll, without which Pages drops _static/
    'myst_parser',
]

# MyST parser settings
myst_heading_anchors = 3

# Napoleon settings for Google/NumPy style docstrings
napoleon_google_docstring = True
napoleon_numpy_docstring = True

# Autodoc settings
autodoc_member_order = 'bysource'
# graflag.mcp_server needs the optional `mcp` extra (Python 3.10+); mocked so
# the reference builds without it.
autodoc_mock_imports = ['mcp', 'anyio', 'pydantic']
autodoc_default_options = {
    'members': True,
    'undoc-members': True,
    'show-inheritance': True,
}

# HTML output
html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']
# Copied verbatim to the site root: redirects for pages that have been retired.
html_extra_path = ['_extra']

# Exclude patterns
exclude_patterns = ['_build']

# Master doc
master_doc = 'index'
