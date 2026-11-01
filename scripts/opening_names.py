"""Selected display names verified against official Chess.com opening pages.

Checked 2026-11-02. Exact full-line overrides take priority over verified
family titles. A canonical family title does not verify its variant suffix.
There is no global Zukertort replacement. The pinned CC0 files stay intact.
"""
from types import MappingProxyType

VERIFIED_ON = "2026-11-02"
REFERENCE_ROOT = "https://www.chess.com/openings/"

# Family boundaries only. The suffix after ':' remains catalogue terminology
# unless a separate exact full-line override below verifies the whole name.
_FAMILY_ROWS = (
    ("King's Pawn Game", "King's Pawn Opening", "Kings-Pawn-Opening"),
    ("Queen's Pawn Game", "Queen's Pawn Opening", "Queens-Pawn-Opening"),
    ("Ruy Lopez", "Ruy Lopez Opening", "Ruy-Lopez-Opening"),
    ("Alekhine Defense", "Alekhine's Defense", "Alekhines-Defense"),
    ("Owen Defense", "Owen's Defense", "Owens-Defense"),
    ("Bird Opening", "Bird's Opening", "Birds-Opening"),
    ("Nimzo-Larsen Attack", "Nimzowitsch-Larsen Attack", "Nimzowitsch-Larsen-Attack"),
    ("Hungarian Opening", "King's Fianchetto Opening", "Kings-Fianchetto-Opening"),
    ("Indian Defense", "Indian Game", "Indian-Game"),
)
FAMILY_DISPLAY_NAMES = MappingProxyType({
    source: (canonical, REFERENCE_ROOT + slug) for source, canonical, slug in _FAMILY_ROWS
})


def family_display_name(name):
    family, separator, suffix = name.partition(":")
    canonical, url = FAMILY_DISPLAY_NAMES.get(family, (family, None))
    return canonical + separator + suffix, url


def catalog_family_name(name):
    """Internal role lookup only; never rewrite arbitrary parts of a name."""
    family, separator, suffix = name.partition(":")
    original = next((source for source, (canonical, _) in FAMILY_DISPLAY_NAMES.items()
                     if canonical == family), family)
    return original + separator + suffix

# ECO, original CC0 name, exact line, verified display name, official page.
_ROWS = (
    ("A06", "Zukertort Opening: Tennison Gambit", "e2e4 d7d5 g1f3", "Réti Opening: Tennison Gambit", "Reti-Opening-Tennison-Gambit"),
    ("A06", "Zukertort Opening: Santasiere's Folly", "b2b4 d7d5 g1f3", "Réti Opening: Santasiere's Folly", "Reti-Opening-Santasieres-Folly"),
    ("E60", "Indian Defense: West Indian Defense", "d2d4 g8f6 c2c4 g7g6", "King's Indian Defense", "Kings-Indian-Defense"),
    ("A00", "Hungarian Opening", "g2g3", "King's Fianchetto Opening", "Kings-Fianchetto-Opening"),
    ("A01", "Nimzo-Larsen Attack", "b2b3", "Nimzowitsch-Larsen Attack", "Nimzowitsch-Larsen-Attack"),
    ("A02", "Bird Opening", "f2f4", "Bird's Opening", "Birds-Opening"),
    ("A04", "Zukertort Opening", "g1f3", "Réti Opening", "Reti-Opening"),
    ("A05", "Zukertort Opening", "g1f3 g8f6", "Réti Opening", "Reti-Opening-1...Nf6"),
    ("A05", "Zukertort Opening", "g1f3 g8f6 b1c3 b8c6", "Réti Opening", "Reti-Opening-1...Nf6-2.Nc3-Nc6"),
    ("A06", "Zukertort Opening", "g1f3 d7d5", "Réti Opening", "Reti-Opening-1...d5"),
    ("A04", "Zukertort Opening: Arctic Defense", "g1f3 f7f6", "Réti Opening: Arctic Defense", "Reti-Opening-Arctic-Defense"),
    ("A04", "Zukertort Opening: Arctic Defense, Drunken Knight Variation", "g1f3 f7f6 e2e4 g8h6 d2d4 h6f7", "Réti Opening: Arctic Defense, Drunken Knight Variation", "Reti-Opening-Arctic-Defense-Drunken-Knight-Variation"),
    ("A04", "Zukertort Opening: Basman Defense", "g1f3 h7h6", "Réti Opening: Basman Defense", "Reti-Opening-Basman-Defense"),
    ("A04", "Zukertort Opening: Black Mustang Defense", "g1f3 b8c6", "Réti Opening: Black Mustang Defense", "Reti-Opening-Black-Mustang-Defense"),
    ("A04", "Zukertort Opening: Dutch Variation", "g1f3 f7f5", "Réti Opening: Dutch Variation", "Reti-Opening-Dutch-Variation"),
    ("A04", "Zukertort Opening: Herrstrom Gambit", "g1f3 g7g5", "Réti Opening: Herrstrom Gambit", "Reti-Opening-Herrstrom-Gambit"),
    ("A04", "Zukertort Opening: Kingside Fianchetto", "g1f3 g7g6", "Réti Opening: Kingside Fianchetto Variation", "Reti-Opening-Kingside-Fianchetto-Variation"),
    ("A04", "Zukertort Opening: Lisitsyn Gambit", "g1f3 f7f5 e2e4", "Réti Opening: Dutch, Lisitsyn Gambit", "Reti-Opening-Dutch-Lisitsyn-Gambit"),
    ("A04", "Zukertort Opening: Lisitsyn Gambit Deferred", "g1f3 f7f5 d2d3 g8f6 e2e4", "Réti Opening: Dutch, Deferred Lisitsyn Gambit", "Reti-Opening-Dutch-Deferred-Lisitsyn-Gambit"),
    ("A04", "Zukertort Opening: Pirc Invitation", "g1f3 d7d6", "Réti Opening: Pirc Invitation", "Reti-Opening-Pirc-Invitation"),
    ("A04", "Zukertort Opening: Polish Defense", "g1f3 b7b5", "Réti Opening: Polish Defense", "Reti-Opening-Polish-Defense"),
    ("A04", "Zukertort Opening: Queen's Gambit Invitation", "g1f3 e7e6", "Réti Opening: Queen's Gambit Invitation", "Reti-Opening-Queens-Gambit-Invitation"),
    ("A04", "Zukertort Opening: Queenside Fianchetto Variation", "g1f3 b7b6", "Réti Opening: Queenside Fianchetto Variation", "Reti-Opening-Queenside-Fianchetto-Variation"),
    ("A04", "Zukertort Opening: Sicilian Invitation", "g1f3 c7c5", "Réti Opening: Sicilian Invitation", "Reti-Opening-Sicilian-Invitation"),
    ("A04", "Zukertort Opening: Slav Invitation", "g1f3 c7c6", "Réti Opening: Slav Invitation", "Reti-Opening-Slav-Invitation"),
    ("A04", "Zukertort Opening: St. George Defense", "g1f3 a7a6", "Réti Opening: St. George Defense", "Reti-Opening-St-George-Defense"),
    ("A04", "Zukertort Opening: Ware Defense", "g1f3 a7a5", "Réti Opening: Ware Defense", "Reti-Opening-Ware-Defense"),
    ("A05", "Zukertort Opening: Nimzo-Larsen Variation", "g1f3 g8f6 b2b3", "Réti Opening: Nimzo-Larsen Variation", "Reti-Opening-Nimzo-Larsen-Variation"),
    ("A05", "Zukertort Opening: Quiet System", "g1f3 g8f6 e2e3", "Réti Opening: Quiet System", "Reti-Opening-Quiet-System"),
    ("A06", "Zukertort Opening: Old Indian Attack", "g1f3 d7d5 d2d3", "Réti Opening: Old Indian Attack", "Reti-Opening-Old-Indian-Attack"),
    ("A06", "Zukertort Opening: Reversed Mexican Defense", "g1f3 d7d5 b1c3", "Réti Opening: Reversed Mexican Defense", "Reti-Opening-Reversed-Mexican-Defense"),
    ("A06", "Zukertort Opening: The Potato", "g1f3 d7d5 a2a4", "Réti Opening: The Potato Variation", "Reti-Opening-The-Potato-Variation"),
    ("A09", "Réti Opening", "g1f3 d7d5 c2c4", "Réti Opening: Réti Gambit", "Reti-Opening-Reti-Gambit"),
    ("A09", "Réti Opening: Advance Variation", "g1f3 d7d5 c2c4 d5d4", "Réti Opening: Réti Gambit Declined, Advance Variation", "Reti-Opening-Reti-Gambit-Declined-Advance-Variation"),
    ("A09", "Réti Opening: Advance Variation, Michel Gambit", "g1f3 d7d5 c2c4 d5d4 b2b4 c7c5", "Réti Opening: Réti Gambit Declined, Advance, Michel Gambit", "Reti-Opening-Reti-Gambit-Declined-Advance-Michel-Gambit"),
    ("A09", "Réti Opening: Penguin Variation", "g1f3 d7d5 c2c4 d5d4 h1g1", "Réti Opening: Réti Gambit Declined, Advance, Penguin Variation", "Reti-Opening-Reti-Gambit-Declined-Advance-Penguin-Variation"),
    ("A09", "Réti Opening: Reversed Blumenfeld Gambit", "g1f3 d7d5 c2c4 d5d4 e2e3 c7c5 b2b4", "Réti Opening: Réti Gambit Declined, Advance, Reversed Blumenfeld Gambit", "Reti-Opening-Reti-Gambit-Declined-Advance-Reversed-Blumenfeld-Gambit"),
    ("A09", "Réti Opening: Réti Accepted", "g1f3 d7d5 c2c4 d5c4", "Réti Opening: Réti Gambit Accepted", "Reti-Opening-Reti-Gambit-Accepted"),
)

DISPLAY_NAME_OVERRIDES = MappingProxyType({
    (eco, source, tuple(line.split())): (display, REFERENCE_ROOT + slug)
    for eco, source, line, display, slug in _ROWS
})

# A verified named endpoint absent from the pinned catalogue. This is consumed
# only by the presentation index, never by the engine's opening candidate index.
# The official page does not provide an ECO code, so none is invented.
SUPPLEMENTAL_DISPLAY_LINES = (
    ("", "Modern Defense", ("d2d4", "d7d6", "c2c4", "g7g6"), REFERENCE_ROOT + "Modern-Defense"),
)


def display_name(eco, source_name, moves):
    exact = DISPLAY_NAME_OVERRIDES.get((eco, source_name, moves))
    if exact is not None:
        return exact
    return family_display_name(source_name)[0], None
