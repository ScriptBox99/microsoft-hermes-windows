#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# -*- coding: utf-8 -*-

# Generates list of unicode ranges belonging to a set of categories
# Usage: genUnicodeTable.py

import datetime
import hashlib
import sys
import urllib.request
from string import Template


UNICODE_DATA_URL = "ftp://ftp.unicode.org/Public/UNIDATA/UnicodeData.txt"
UNICODE_SPECIAL_CASING_URL = "ftp://ftp.unicode.org/Public/UNIDATA/SpecialCasing.txt"
UNICODE_CASE_FOLDING_URL = "ftp://ftp.unicode.org/Public/UNIDATA/CaseFolding.txt"


# Unicode data field indexes. See UnicodeData.txt.
CODEPOINT_FIELD = 0
GENERAL_CATEGORY_FIELD = 2
UPPERCASE_FIELD = 12
LOWERCASE_FIELD = 13


def print_template(s, **kwargs):
    """Substitute in the keyword arguments to the template string
    (or direct template) s, and print the result, followed by a
    newline.
    """
    text = Template(s).substitute(**kwargs)
    print(text.strip())
    print("")


def print_header(unicodedata_sha256, specialcasing_sha256, casefolding_sha256):
    print_template(
        """
//
// File generated by genUnicodeTable.py
// using Unicode data files downloaded on ${today}
// UnicodeData.txt SHA256:   ${unicodedata_sha256}
// SpecialCasing.txt SHA256: ${specialcasing_sha256}
// CaseFolding.txt SHA256:   ${casefolding_sha256}
// *** DO NOT EDIT BY HAND ***

/// An inclusive range of Unicode characters.
struct UnicodeRange { uint32_t first; uint32_t second; };

/// A UnicodeTransformRange expresses a mapping such as case folding.
/// A character cp is mapped to cp + delta if cp is 0 for the given modulus.
struct UnicodeTransformRange {
    /// The first codepoint of the range.
    unsigned start:24;

    /// The number of characters in the range.
    unsigned count:8;

    /// The signed delta amount.
    int delta:24;

    /// The modulo amount.
    unsigned modulo:8;
};
""",
        today=str(datetime.date.today()),
        unicodedata_sha256=unicodedata_sha256,
        specialcasing_sha256=specialcasing_sha256,
        casefolding_sha256=casefolding_sha256,
    )


def run_interval(unicode_data_lines, args):
    name = args[0]
    categories = set(args[1:])
    begin = 0
    intervals = []
    last_cp = 0
    openi = False
    for line in unicode_data_lines:
        fields = line.split(";")
        cp_str, category = fields[CODEPOINT_FIELD], fields[GENERAL_CATEGORY_FIELD]
        cp = int(cp_str, 16)
        if category in categories:
            if not openi:
                begin = cp
                openi = True
            else:
                pass  # do nothing we are still in interval
        else:
            if openi:
                intervals.append((begin, last_cp))
                openi = False
            else:
                pass  # keep looking
        last_cp = cp

    if openi:
        intervals.append((begin, last_cp))

    print_template(
        """
// ${args}
// static constexpr uint32_t ${name}_SIZE = $interval_count;
static constexpr UnicodeRange ${name}[] = {
${intervals}
};
    """,
        args=" ".join(args),
        name=name,
        interval_count=len(intervals),
        intervals="\n".join(
            "{" + hex(i[0]) + ", " + hex(i[1]) + "}," for i in intervals
        ),
    )


def print_categories(unicode_data_lines):
    """Output UnicodeRanges for Unicode General Categories."""
    categories = [
        "UNICODE_LETTERS Lu Ll Lt Lm Lo Nl",
        "UNICODE_COMBINING_MARK Mn Mc",
        "UNICODE_DIGIT Nd",
        "UNICODE_CONNECTOR_PUNCTUATION Pc",
    ]
    for cat in categories:
        run_interval(unicode_data_lines, cat.split())


def stride_from(p1, p2):
    return p2[0] - p1[0]


def delta_within(p):
    return p[1] - p[0]


def as_hex(cp):
    return "0x%.4X" % cp


class DeltaMapBlock(object):
    def __init__(self):
        self.pairs = []

    def stride(self):
        return stride_from(self.pairs[0], self.pairs[1])

    def delta(self):
        return delta_within(self.pairs[0])

    def can_append(self, pair):
        if not self.pairs:
            return True
        if pair[0] - self.pairs[0][0] >= 256:
            return False
        if self.delta() != delta_within(pair):
            return False
        return len(self.pairs) < 2 or self.stride() == stride_from(self.pairs[-1], pair)

    @staticmethod
    def append_to_list(blocks, p):
        if not blocks or not blocks[-1].can_append(p):
            blocks.append(DeltaMapBlock())
        blocks[-1].pairs.append(p)

    def output(self):
        pairs = self.pairs
        if not pairs:
            return ""

        first = pairs[0][0]
        last = pairs[-1][0]
        modulo = self.stride() if len(pairs) >= 2 else 1
        delta = self.delta()
        code = Template("{$first, $count, $delta, $modulo}").substitute(
            first=as_hex(first), count=last - first + 1, delta=delta, modulo=modulo
        )
        return code.strip()


class CaseMap(object):
    """Unicode case mapping helper.

    This class holds the list of codepoints, and their uppercase and
    lowercase mappings.

    """

    def __init__(self, unicode_data_lines, special_casing_lines, casefolding_lines):
        """Construct with the lines from UnicodeData and SpecialCasing."""
        self.toupper = {}
        self.tolower = {}
        self.codepoints = []
        for line in unicode_data_lines:
            fields = line.split(";")
            self.__set_casemap(
                fields[CODEPOINT_FIELD],
                upper=fields[UPPERCASE_FIELD],
                lower=fields[LOWERCASE_FIELD],
            )
        self.codepoints.extend(self.toupper.keys())

        # Apply special cases. This is to support ES5.1 Canonicalize, which is
        # cast in terms of toUpperCase(). The desire here is to have a
        # locale-independent result. Thus we ignore SpecialCasing rules that
        # are locale specific. We can also get away with ignoring
        # context-sensitive rules because Canonicalize only considers one
        # character. Thus ignore any rules that have a condition.
        # Format is codepoint, lower, title, upper, condition
        for line in special_casing_lines:
            # Trim comments
            line = line.split("#")[0]
            fields = line.split(";")
            if len(fields) < 5:
                continue
            cps, lower, title, upper, condition = fields[:5]
            # Title is unused
            _ = title  # noqa: F841
            if not condition.strip():
                self.__set_casemap(cps, upper=upper, lower=lower)

        # Characters default to folding to themselves.
        self.folds = {cp: cp for cp in self.codepoints}

        # Parse case folds.
        for line in casefolding_lines:
            fields = line.split("#")[0].split(";")
            if len(fields) != 4:
                continue
            orig, status, folded, _ = map(str.strip, fields)
            # We are only interested in common and simple case foldings.
            if status not in ["C", "S"]:
                continue
            self.folds[int(orig, 16)] = int(folded, 16)

    def __set_casemap(self, cp, upper, lower):
        """Set a case mapping.

        Mark the upper and lower case forms of cp. If a form is empty,
        the character is its own case mapping.
        All parameters are code points encoded via hex into a string.

        """
        # Parse the codepoint from hex.
        cp = int(cp, 16)

        # "The simple uppercase is omitted in the data file if the uppercase
        # is the same as the code point itself."
        # The same is true for the lowercase.
        # Skip eszett or anything else that maps to more than one character.
        self.toupper[cp] = int(upper, 16) if upper and len(upper.split()) == 1 else cp
        self.tolower[cp] = int(lower, 16) if lower and len(lower.split()) == 1 else cp

    def canonicalize(self, ch, unicode):
        """Canonicalize a character per ES9 21.2.2.8.2."""
        if unicode:
            return self.folds[ch]
        else:
            upper_ch = self.toupper[ch]
            # "If u does not consist of a single character, return ch"
            # We only store 1-1 mappings.
            # "If ch's code unit value is greater than or equal to decimal 128
            # and cu's code unit value is less than decimal 128, then return ch"
            # That is, only ASCII may canonicalize to ASCII.
            if upper_ch < 128 and ch >= 128:
                return ch
            return upper_ch


def print_canonicalizations(casemap, unicode):
    blocks = []
    for cp in casemap.codepoints:
        # legacy does not decode surrogate pairs, so we can skip large code points.
        if not unicode and cp > 0xFFFF:
            continue
        canon_cp = casemap.canonicalize(cp, unicode)
        if cp != canon_cp:
            DeltaMapBlock.append_to_list(blocks, (cp, canon_cp))

    print_template(
        """
// static constexpr uint32_t ${name}_SIZE = ${entry_count};
static constexpr UnicodeTransformRange ${name}[] = {
${entry_text}
};
""",
        name="UNICODE_FOLDS" if unicode else "LEGACY_CANONS",
        entry_count=len(blocks),
        entry_text=",\n".join(b.output() for b in blocks),
    )


if __name__ == "__main__":
    print("Fetching %s..." % UNICODE_DATA_URL, file=sys.stderr)
    with urllib.request.urlopen(UNICODE_DATA_URL) as f:
        unicode_data = f.read()

    print("Fetching %s..." % UNICODE_SPECIAL_CASING_URL, file=sys.stderr)
    with urllib.request.urlopen(UNICODE_SPECIAL_CASING_URL) as f:
        special_casing = f.read()

    print("Fetching %s..." % UNICODE_CASE_FOLDING_URL, file=sys.stderr)
    with urllib.request.urlopen(UNICODE_CASE_FOLDING_URL) as f:
        case_folding = f.read()

    print_header(
        hashlib.sha256(unicode_data).hexdigest(),
        hashlib.sha256(special_casing).hexdigest(),
        hashlib.sha256(case_folding).hexdigest(),
    )
    udata_lines = unicode_data.decode("utf-8").splitlines()
    special_lines = special_casing.decode("utf-8").splitlines()
    casefolding_lines = case_folding.decode("utf-8").splitlines()
    casemap = CaseMap(
        unicode_data_lines=udata_lines,
        special_casing_lines=special_lines,
        casefolding_lines=casefolding_lines,
    )
    print_categories(udata_lines)
    print_canonicalizations(casemap, unicode=True)
    print_canonicalizations(casemap, unicode=False)
