"""LinkedIn Connections.csv preamble handling.

A Connections.csv straight from LinkedIn's "Export Your Data" does not begin
with the header row — it prepends a notes block. Feeding that to DictReader
makes "Notes:" the only fieldname, so every row yields empty names and the
import silently skips all of them: a zero-record "success" on a good file.
"""
import pytest

from api.services.people_aggregator import load_linkedin_connections

pytestmark = pytest.mark.unit


HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
ROWS = (
    'Jonathan,"Shaffer, Ph.D.",https://www.linkedin.com/in/jonshaffer,,'
    'University of Vermont,Assistant Professor of Sociology,24 Jun 2026\n'
    "Jade,Martinez,https://www.linkedin.com/in/jade-martinez,,SEIU,"
    "Senior Coordinator,21 Jun 2026\n"
)
PREAMBLE = (
    "Notes:\n"
    '"When exporting your connection data, you may notice that some of the '
    'email addresses are missing. You will only see email addresses for '
    'connections who have allowed this."\n'
    "\n"
)


def _write(tmp_path, text):
    p = tmp_path / "Connections.csv"
    p.write_text(text, encoding="utf-8")
    return str(p)


class TestLinkedInPreamble:

    def test_raw_export_with_preamble_is_parsed(self, tmp_path):
        conns = load_linkedin_connections(_write(tmp_path, PREAMBLE + HEADER + ROWS))
        assert len(conns) == 2
        assert conns[0]["first_name"] == "Jonathan"
        assert conns[0]["last_name"] == "Shaffer, Ph.D."
        assert conns[0]["company"] == "University of Vermont"
        assert conns[1]["first_name"] == "Jade"

    def test_already_stripped_file_still_works(self, tmp_path):
        """Files de-preambled by hand must keep working."""
        conns = load_linkedin_connections(_write(tmp_path, HEADER + ROWS))
        assert len(conns) == 2
        assert conns[0]["first_name"] == "Jonathan"

    def test_quoted_comma_in_last_name_survives(self, tmp_path):
        """The preamble skip must not break normal CSV quoting."""
        conns = load_linkedin_connections(_write(tmp_path, PREAMBLE + HEADER + ROWS))
        assert conns[0]["last_name"] == "Shaffer, Ph.D."

    def test_linkedin_url_and_connected_on_mapped(self, tmp_path):
        conns = load_linkedin_connections(_write(tmp_path, PREAMBLE + HEADER + ROWS))
        assert conns[0]["linkedin_url"].endswith("/jonshaffer")
        assert conns[0]["connected_on"] == "24 Jun 2026"

    def test_file_without_header_does_not_consume_rows(self, tmp_path):
        """No recognisable header: fall back, don't silently eat the file."""
        conns = load_linkedin_connections(_write(tmp_path, "a,b,c\n1,2,3\n"))
        assert conns == [] or all(not c["first_name"] for c in conns)

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_linkedin_connections(str(tmp_path / "nope.csv")) == []
