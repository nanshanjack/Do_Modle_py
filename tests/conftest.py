"""共享 pytest 夹具（构造器与常量见 ``tests/_helpers.py``）。"""

from __future__ import annotations

import pytest

from do_modle.objects import Board
from tests._helpers import (
    BSE_INSTRUMENT,
    GEM_INSTRUMENT,
    MAIN_INSTRUMENT,
    STAR_INSTRUMENT,
    make_bar,
)


@pytest.fixture
def main_board_instrument():
    """招商轮船：主板、非 ST、2006 年上市。"""
    return MAIN_INSTRUMENT


@pytest.fixture
def gem_instrument():
    return GEM_INSTRUMENT


@pytest.fixture
def star_instrument():
    return STAR_INSTRUMENT


@pytest.fixture
def bse_instrument():
    return BSE_INSTRUMENT


@pytest.fixture
def bar_factory():
    return make_bar


@pytest.fixture
def board_enum():
    return Board
