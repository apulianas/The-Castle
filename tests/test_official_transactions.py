from datetime import date

from ravens_bot.official_transactions import (
    merge_standard_elevations,
    parse_standard_elevations,
)
from ravens_bot.models import Transaction


PAGE = """
<table>
  <thead><tr><th class="nfl-c-transactions-report__month">October</th></tr></thead>
  <tbody>
    <tr>
      <td class="nfl-c-transactions-report__date">10/12</td>
      <td><p>Waived S Reuben Lowery III. Signed DL C.J. Okoye to the active
        roster from the practice squad. Activated QB Tyler Huntley and S
        Keondre Jackson from the practice squad (standard elevation) vs. LAR.</p></td>
    </tr>
    <tr>
      <td class="nfl-c-transactions-report__date">10/05</td>
      <td><p>Activated QB Tyler Huntley and DL C.J. Okoye from the practice
        squad (standard elevation) vs. Hou.</p></td>
    </tr>
  </tbody>
</table>
"""


def test_parse_standard_elevations_reads_only_the_requested_days_call_up() -> None:
    transactions = parse_standard_elevations(PAGE, date(2025, 10, 12))

    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.type_text == "Activated"
    assert transaction.description == (
        "Activated QB Tyler Huntley and S Keondre Jackson from the practice "
        "squad (standard elevation) vs. LAR."
    )
    assert [(player.name, player.position) for player in transaction.players] == [
        ("Tyler Huntley", "QB"),
        ("Keondre Jackson", "S"),
    ]


def test_merge_standard_elevations_does_not_repeat_an_existing_item() -> None:
    elevation = parse_standard_elevations(PAGE, date(2025, 10, 12))[0]
    espn_copy = Transaction(
        transaction_id="espn",
        date=elevation.date,
        description=elevation.description,
        type_text=elevation.type_text,
        players=elevation.players,
    )

    assert merge_standard_elevations([espn_copy], [elevation]) == [espn_copy]
