"""
Install specifications for every repo+version in SWE-bench Lite.

Sourced directly from:
  https://github.com/swe-bench/SWE-bench/blob/main/swebench/harness/constants/python.py

Each spec is a dict with:
  python       : str   — Python version for conda env
  pip_packages : list  — packages to pip-install BEFORE the editable install
  install      : str   — the editable install command (run inside /repo)
  packages     : str   — conda packages or "requirements.txt" (optional)
  pre_install  : list  — shell commands to run BEFORE pip_packages (optional)

Usage:
    from .swe_specs import get_spec
    spec = get_spec("astropy/astropy", "4.3")
    # spec["python"]       -> "3.9"
    # spec["pip_packages"] -> ["numpy==1.25.2", "setuptools==68.0.0", ...]
    # spec["install"]      -> "python -m pip install -e .[test] --verbose"
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Raw specs (copy from SWE-bench constants/python.py, trimmed to Lite repos)
# ---------------------------------------------------------------------------

_TEST_PYTEST   = "pytest --no-header -rA --tb=no -p no:cacheprovider"
_TEST_ASTROPY  = "pytest -rA -vv -o console_output_style=classic --tb=no"
_TEST_DJANGO   = "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1"
_TEST_SYMPY    = "bin/test -C --verbose"
_TEST_SPHINX   = "tox --current-env -epy39 -v --"
_TEST_SEABORN  = "pytest --no-header -rA"

# ── astropy ─────────────────────────────────────────────────────────────────
_ASTROPY_NEW_PKGS = [
    "attrs==23.1.0", "exceptiongroup==1.1.3", "execnet==2.0.2",
    "hypothesis==6.82.6", "iniconfig==2.0.0", "numpy==1.25.2",
    "packaging==23.1", "pluggy==1.3.0", "psutil==5.9.5",
    "pyerfa==2.0.0.3", "pytest-arraydiff==0.5.0",
    "pytest-astropy-header==0.2.2", "pytest-astropy==0.10.0",
    "pytest-cov==4.1.0", "pytest-doctestplus==1.0.0",
    "pytest-filter-subpackage==0.1.2", "pytest-mock==3.11.1",
    "pytest-openfiles==0.5.0", "pytest-remotedata==0.4.0",
    "pytest-xdist==3.3.1", "pytest==7.4.0", "PyYAML==6.0.1",
    "setuptools==68.0.0", "sortedcontainers==2.4.0", "tomli==2.0.1",
]
_ASTROPY_OLD_PKGS = [
    "attrs==17.3.0", "exceptiongroup==0.0.0a0", "execnet==1.5.0",
    "hypothesis==3.44.2", "cython==0.27.3", "jinja2==2.10",
    "MarkupSafe==1.0", "numpy==1.16.0", "packaging==16.8",
    "pluggy==0.6.0", "psutil==5.4.2", "pyerfa==1.7.0",
    "pytest-arraydiff==0.1", "pytest-astropy-header==0.1",
    "pytest-astropy==0.2.1", "pytest-cov==2.5.1",
    "pytest-doctestplus==0.1.2", "pytest-filter-subpackage==0.1",
    "pytest-forked==0.2", "pytest-mock==1.6.3",
    "pytest-openfiles==0.2.0", "pytest-remotedata==0.2.0",
    "pytest-xdist==1.20.1", "pytest==3.3.1", "PyYAML==3.12",
    "sortedcontainers==1.5.9", "tomli==0.2.0",
]
_ASTROPY_PRE_INSTALL_PATCH = [
    r"""sed -i 's/requires = \["setuptools",/requires = \["setuptools==68.0.0",/' pyproject.toml"""
]

SPECS_ASTROPY: dict[str, dict] = {
    k: {
        "python": "3.9",
        "install": "python -m pip install -e .[test] --verbose",
        "pip_packages": _ASTROPY_NEW_PKGS,
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["3.0", "3.1", "3.2", "4.1", "4.2", "4.3",
               "5.0", "5.1", "5.2", "v5.3"]
}
SPECS_ASTROPY.update({
    k: {
        "python": "3.6",
        "install": "python -m pip install -e .[test] --verbose",
        "packages": "setuptools==38.2.4",
        "pip_packages": _ASTROPY_OLD_PKGS,
        "test_cmd": _TEST_ASTROPY,
    }
    for k in ["0.1", "0.2", "0.3", "0.4", "1.1", "1.2", "1.3"]
})
for k in ["4.1", "4.2", "4.3", "5.0", "5.1", "5.2", "v5.3"]:
    SPECS_ASTROPY[k]["pre_install"] = _ASTROPY_PRE_INSTALL_PATCH
SPECS_ASTROPY["v5.3"]["python"] = "3.10"

# ── sympy ────────────────────────────────────────────────────────────────────
SPECS_SYMPY: dict[str, dict] = {
    k: {
        "python": "3.9",
        "packages": "mpmath flake8",
        "pip_packages": ["mpmath==1.3.0", "flake8-comprehensions"],
        "install": "python -m pip install -e .",
        "test_cmd": _TEST_SYMPY,
    }
    for k in ["0.7", "1.0", "1.1", "1.2", "1.4", "1.5", "1.6",
               "1.7", "1.8", "1.9", "1.10", "1.11", "1.12"]
}
SPECS_SYMPY.update({
    k: {
        "python": "3.9",
        "packages": "requirements.txt",
        "install": "python -m pip install -e .",
        "pip_packages": ["mpmath==1.3.0"],
        "test_cmd": _TEST_SYMPY,
    }
    for k in ["1.13", "1.14"]
})

# ── django ───────────────────────────────────────────────────────────────────
SPECS_DJANGO: dict[str, dict] = {
    k: {
        "python": "3.5",
        "packages": "requirements.txt",
        "pre_install": [
            "apt-get update && apt-get install -y locales",
            "echo 'en_US UTF-8' > /etc/locale.gen",
            "locale-gen en_US.UTF-8",
        ],
        "install": "python setup.py install",
        "pip_packages": ["setuptools"],
        "test_cmd": _TEST_DJANGO,
    }
    for k in ["1.7", "1.8", "1.9", "1.10", "1.11", "2.0", "2.1", "2.2"]
}
SPECS_DJANGO.update({
    k: {"python": "3.6", "packages": "requirements.txt",
        "install": "python -m pip install -e .", "test_cmd": _TEST_DJANGO}
    for k in ["3.0", "3.1", "3.2"]
})
SPECS_DJANGO.update({
    k: {"python": "3.8", "packages": "requirements.txt",
        "install": "python -m pip install -e .", "test_cmd": _TEST_DJANGO}
    for k in ["4.0"]
})
SPECS_DJANGO.update({
    k: {"python": "3.9", "packages": "requirements.txt",
        "install": "python -m pip install -e .", "test_cmd": _TEST_DJANGO}
    for k in ["4.1", "4.2"]
})
SPECS_DJANGO.update({
    k: {"python": "3.11", "packages": "requirements.txt",
        "install": "python -m pip install -e .", "test_cmd": _TEST_DJANGO}
    for k in ["5.0", "5.1", "5.2"]
})

# ── flask ────────────────────────────────────────────────────────────────────
SPECS_FLASK: dict[str, dict] = {
    "2.0": {
        "python": "3.9", "packages": "requirements.txt",
        "install": "python -m pip install -e .",
        "pip_packages": ["setuptools==70.0.0", "Werkzeug==2.3.7",
                         "Jinja2==3.0.1", "itsdangerous==2.1.2",
                         "click==8.0.1", "MarkupSafe==2.1.3"],
        "test_cmd": _TEST_PYTEST,
    },
    "2.1": {
        "python": "3.10", "packages": "requirements.txt",
        "install": "python -m pip install -e .",
        "pip_packages": ["setuptools==70.0.0", "click==8.1.3",
                         "itsdangerous==2.1.2", "Jinja2==3.1.2",
                         "MarkupSafe==2.1.1", "Werkzeug==2.3.7"],
        "test_cmd": _TEST_PYTEST,
    },
}
SPECS_FLASK.update({
    k: {
        "python": "3.11", "packages": "requirements.txt",
        "install": "python -m pip install -e .",
        "pip_packages": ["setuptools==70.0.0", "click==8.1.3",
                         "itsdangerous==2.1.2", "Jinja2==3.1.2",
                         "MarkupSafe==2.1.1", "Werkzeug==2.3.7"],
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["2.2", "2.3", "3.0", "3.1"]
})

# ── requests ─────────────────────────────────────────────────────────────────
SPECS_REQUESTS: dict[str, dict] = {
    k: {
        "python": "3.9", "packages": "pytest",
        "install": "python -m pip install .",
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["0.7", "0.8", "0.9", "0.11", "0.13", "0.14", "1.1", "1.2",
               "2.0", "2.2", "2.3", "2.4", "2.5", "2.7", "2.8", "2.9",
               "2.10", "2.11", "2.12", "2.17", "2.18", "2.19", "2.22",
               "2.25", "2.26", "2.27", "2.31", "3.0"]
}

# ── pytest ───────────────────────────────────────────────────────────────────
SPECS_PYTEST: dict[str, dict] = {
    k: {"python": "3.9", "install": "python -m pip install -e .",
        "test_cmd": _TEST_PYTEST}
    for k in ["4.4", "4.5", "4.6", "5.0", "5.1", "5.2", "5.3", "5.4",
               "6.0", "6.2", "6.3", "7.0", "7.1", "7.2", "7.4",
               "8.0", "8.1", "8.2", "8.3", "8.4"]
}
SPECS_PYTEST["4.4"]["pip_packages"] = [
    "atomicwrites==1.4.1", "attrs==23.1.0", "more-itertools==10.1.0",
    "pluggy==0.13.1", "py==1.11.0", "setuptools==68.0.0", "six==1.16.0",
]
SPECS_PYTEST["4.5"]["pip_packages"] = [
    "atomicwrites==1.4.1", "attrs==23.1.0", "more-itertools==10.1.0",
    "pluggy==0.11.0", "py==1.11.0", "setuptools==68.0.0",
    "six==1.16.0", "wcwidth==0.2.6",
]
for k in ["4.6", "5.0", "5.1", "5.2", "5.3", "5.4"]:
    SPECS_PYTEST[k]["pip_packages"] = [
        "atomicwrites==1.4.1", "attrs==23.1.0", "more-itertools==10.1.0",
        "packaging==23.1", "pluggy==0.13.1", "py==1.11.0", "wcwidth==0.2.6",
    ]
for k in ["6.0", "6.2", "6.3", "7.0", "7.1", "7.2"]:
    SPECS_PYTEST[k]["pip_packages"] = [
        "attrs==23.1.0", "iniconfig==2.0.0", "packaging==23.1",
        "pluggy==0.13.1", "py==1.11.0", "toml==0.10.2",
    ]
for k in ["7.4", "8.0", "8.1", "8.2", "8.3", "8.4"]:
    SPECS_PYTEST[k]["pip_packages"] = [
        "iniconfig==2.0.0", "packaging==23.1", "pluggy==1.3.0",
        "exceptiongroup==1.1.3", "tomli==2.0.1",
    ]

# ── matplotlib ───────────────────────────────────────────────────────────────
_MPL_PRE_NEW = [
    "apt-get -y update && apt-get -y upgrade && DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y imagemagick ffmpeg texlive texlive-latex-extra "
    "texlive-fonts-recommended texlive-xetex texlive-luatex cm-super dvipng",
]
_MPL_PKGS_NEW = [
    "contourpy==1.1.0", "cycler==0.11.0", "fonttools==4.42.1",
    "ghostscript", "kiwisolver==1.4.5", "numpy==1.25.2",
    "packaging==23.1", "pillow==10.0.0", "pikepdf", "pyparsing==3.0.9",
    "python-dateutil==2.8.2", "six==1.16.0", "setuptools==68.1.2",
    "setuptools-scm==7.1.0", "typing-extensions==4.7.1",
]
SPECS_MATPLOTLIB: dict[str, dict] = {
    k: {
        "python": "3.11", "packages": "environment.yml",
        "install": "python -m pip install -e .",
        "pre_install": _MPL_PRE_NEW,
        "pip_packages": _MPL_PKGS_NEW,
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["3.5", "3.6", "3.7", "3.8", "3.9"]
}
SPECS_MATPLOTLIB.update({
    k: {
        "python": "3.8", "packages": "requirements.txt",
        "install": "python -m pip install -e .",
        "pre_install": [
            "apt-get -y update && apt-get -y upgrade && DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y imagemagick ffmpeg libfreetype6-dev pkg-config texlive "
            "texlive-latex-extra texlive-fonts-recommended texlive-xetex texlive-luatex cm-super",
        ],
        "pip_packages": ["pytest", "ipython"],
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["3.1", "3.2", "3.3", "3.4"]
})
for k in ["3.8", "3.9"]:
    SPECS_MATPLOTLIB[k]["install"] = 'python -m pip install --no-build-isolation -e ".[dev]"'

# ── seaborn ──────────────────────────────────────────────────────────────────
_SEABORN_PKGS = [
    "contourpy==1.1.0", "cycler==0.11.0", "fonttools==4.42.1",
    "importlib-resources==6.0.1", "kiwisolver==1.4.5", "matplotlib==3.7.2",
    "numpy==1.25.2", "packaging==23.1", "pandas==1.3.5", "pillow==10.0.0",
    "pyparsing==3.0.9", "pytest", "python-dateutil==2.8.2",
    "pytz==2023.3.post1", "scipy==1.11.2", "six==1.16.0",
    "tzdata==2023.1", "zipp==3.16.2",
]
SPECS_SEABORN: dict[str, dict] = {
    "0.11": {
        "python": "3.9", "install": "python -m pip install -e .",
        "pip_packages": _SEABORN_PKGS, "test_cmd": _TEST_SEABORN,
    }
}
SPECS_SEABORN.update({
    k: {
        "python": "3.9",
        "install": "python -m pip install -e .[dev]",
        "pip_packages": [p.replace("pandas==1.3.5", "pandas==2.0.0")
                         for p in _SEABORN_PKGS],
        "test_cmd": _TEST_SEABORN,
    }
    for k in ["0.12", "0.13", "0.14"]
})

# ── sphinx ───────────────────────────────────────────────────────────────────
SPECS_SPHINX: dict[str, dict] = {
    k: {
        "python": "3.9",
        "pip_packages": ["tox==4.16.0", "tox-current-env==0.0.11", "Jinja2==3.0.3"],
        "install": "python -m pip install -e .[test]",
        "pre_install": ["sed -i 's/pytest/pytest -rA/' tox.ini"],
        "test_cmd": _TEST_SPHINX,
    }
    for k in [
        "1.5", "1.6", "1.7", "1.8", "2.0", "2.1", "2.2", "2.3", "2.4",
        "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "4.0", "4.1", "4.2",
        "4.3", "4.4", "4.5", "5.0", "5.1", "5.2", "5.3", "6.0", "6.2",
        "7.0", "7.1", "7.2", "7.3", "7.4", "8.0", "8.1",
    ]
}
for k in ["8.0", "8.1"]:
    SPECS_SPHINX[k]["python"] = "3.10"

# ── scikit-learn ──────────────────────────────────────────────────────────────
SPECS_SKLEARN: dict[str, dict] = {
    k: {
        "python": "3.6",
        "packages": "numpy scipy cython pytest pandas matplotlib",
        "install": "python -m pip install -v --no-use-pep517 --no-build-isolation -e .",
        "pip_packages": ["cython", "numpy==1.19.2", "setuptools", "scipy==1.5.2"],
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["0.20", "0.21", "0.22"]
}
SPECS_SKLEARN.update({
    k: {
        "python": "3.9",
        "packages": "'numpy==1.19.2' 'scipy==1.5.2' 'cython==3.0.10' pytest "
                    "'pandas<2.0.0' 'matplotlib<3.9.0' setuptools pytest joblib threadpoolctl",
        "install": "python -m pip install -v --no-use-pep517 --no-build-isolation -e .",
        "pip_packages": ["cython", "setuptools", "numpy", "scipy"],
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["1.3", "1.4", "1.5", "1.6"]
})

# ── xarray ───────────────────────────────────────────────────────────────────
SPECS_XARRAY: dict[str, dict] = {
    k: {
        "python": "3.10",
        "packages": "environment.yml",
        "install": "python -m pip install -e .",
        "pip_packages": [
            "numpy==1.23.0", "packaging==23.1", "pandas==1.5.3",
            "pytest==7.4.0", "python-dateutil==2.8.2", "pytz==2023.3",
            "six==1.16.0", "scipy==1.11.1", "setuptools==68.0.0",
            "dask==2022.8.1",
        ],
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["0.12", "0.18", "0.19", "0.20", "2022.03",
               "2022.06", "2022.09", "2023.07", "2024.05"]
}

# ── pvlib ────────────────────────────────────────────────────────────────────
SPECS_PVLIB: dict[str, dict] = {
    k: {
        "python": "3.9",
        "install": "python -m pip install -e .[test]",
        "pip_packages": ["scipy", "pandas", "numpy"],
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["0.9", "0.10", "0.11"]
}

# ── pydicom ───────────────────────────────────────────────────────────────────
SPECS_PYDICOM: dict[str, dict] = {
    k: {
        "python": "3.9",
        "install": "python -m pip install -e .",
        "pip_packages": ["pytest"],
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["1.0", "1.1", "1.2", "1.3", "1.4", "2.0", "2.1", "2.2", "2.3", "2.4"]
}

# ── marshmallow ───────────────────────────────────────────────────────────────
SPECS_MARSHMALLOW: dict[str, dict] = {
    k: {
        "python": "3.9",
        "install": "python -m pip install -e .[dev]",
        "pip_packages": ["pytest"],
        "test_cmd": _TEST_PYTEST,
    }
    for k in ["2.18", "2.19", "2.20", "2.21", "3.0", "3.1", "3.2",
               "3.3", "3.4", "3.5", "3.6", "3.7", "3.8", "3.9",
               "3.10", "3.11", "3.12", "3.13", "3.14", "3.15",
               "3.16", "3.17", "3.18", "3.19", "3.20", "3.21"]
}

# ── pylint / astroid ──────────────────────────────────────────────────────────
SPECS_PYLINT: dict[str, dict] = {
    k: {
        "python": "3.9", "packages": "requirements.txt",
        "install": "python -m pip install -e .", "test_cmd": _TEST_PYTEST,
    }
    for k in ["2.8", "2.9", "2.10", "2.11", "2.13", "2.14", "2.15",
               "2.16", "2.17", "3.0", "3.1", "3.2", "3.3", "4.0"]
}
for k in ["3.0", "3.1", "3.2", "3.3", "4.0"]:
    SPECS_PYLINT[k]["pip_packages"] = ["astroid==3.0.0a6", "setuptools"]

SPECS_ASTROID: dict[str, dict] = {
    k: {
        "python": "3.9", "packages": "requirements.txt",
        "install": "python -m pip install -e .", "test_cmd": _TEST_PYTEST,
    }
    for k in ["2.5", "2.6", "2.7", "2.8", "2.9", "2.10", "2.11",
               "2.12", "2.13", "2.14", "2.15", "3.0", "3.1", "3.2"]
}

# ── sqlfluff ──────────────────────────────────────────────────────────────────
SPECS_SQLFLUFF: dict[str, dict] = {
    k: {
        "python": "3.9", "packages": "requirements.txt",
        "install": "python -m pip install -e .", "test_cmd": _TEST_PYTEST,
    }
    for k in ["0.4", "0.5", "0.6", "0.8", "0.9", "0.10", "0.11", "0.12",
               "0.13", "1.0", "1.1", "1.2", "1.3", "1.4", "2.0", "2.1", "2.2"]
}

# ---------------------------------------------------------------------------
# Master lookup
# ---------------------------------------------------------------------------

MAP_REPO_TO_SPECS: dict[str, dict] = {
    "astropy/astropy":              SPECS_ASTROPY,
    "django/django":                SPECS_DJANGO,
    "matplotlib/matplotlib":        SPECS_MATPLOTLIB,
    "marshmallow-code/marshmallow": SPECS_MARSHMALLOW,
    "mwaskom/seaborn":              SPECS_SEABORN,
    "pallets/flask":                SPECS_FLASK,
    "psf/requests":                 SPECS_REQUESTS,
    "pvlib/pvlib-python":           SPECS_PVLIB,
    "pydata/xarray":                SPECS_XARRAY,
    "pydicom/pydicom":              SPECS_PYDICOM,
    "pylint-dev/astroid":           SPECS_ASTROID,
    "pylint-dev/pylint":            SPECS_PYLINT,
    "pytest-dev/pytest":            SPECS_PYTEST,
    "scikit-learn/scikit-learn":    SPECS_SKLEARN,
    "sphinx-doc/sphinx":            SPECS_SPHINX,
    "sqlfluff/sqlfluff":            SPECS_SQLFLUFF,
    "sympy/sympy":                  SPECS_SYMPY,
}

_FALLBACK_SPEC = {
    "python": "3.11",
    "pip_packages": [],
    "install": "python -m pip install -e .",
}


def get_spec(repo: str, version: str) -> dict:
    """Return the install spec for a given repo + version.

    Falls back gracefully if repo or version is unknown.
    """
    repo_specs = MAP_REPO_TO_SPECS.get(repo)
    if repo_specs is None:
        return dict(_FALLBACK_SPEC)
    spec = repo_specs.get(version)
    if spec is None:
        # Try without patch component: "5.0.1" -> "5.0"
        short = ".".join(version.split(".")[:2])
        spec = repo_specs.get(short)
    if spec is None:
        return dict(_FALLBACK_SPEC)
    return dict(spec)