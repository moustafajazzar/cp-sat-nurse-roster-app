"""`GET /roster` reads the month as it stands once (`solves.input.assemble`), whatever the roster's state: what changed since the
solve and since the submit, the check of an edited or outdated draft and the editor all share that reading. On a large unit a
reading is most of the answer's time (ICU's 64 nurses: about 250 ms each on the seeded stack), so a second one doubled it."""
import pytest

from accounts.models import Role
from rosters.tests.test_pins import pin, saved
from rosters.tests.test_review import BADR_OFF, approve, month_of, ok, record_leave, solved, submit
from solves import input as solve_input

pytestmark = pytest.mark.django_db

# The four-nurse sample's month answers in 67-70 queries here. Read twice (a draft as solved, an approved roster) it took 101;
# three times (an edited or outdated draft, a submitted roster), 140 and 170.
QUERIES = 75


@pytest.fixture(autouse=True)
def _scene(before_june, solver):
    pass


@pytest.fixture
def readings(monkeypatch):
    """How many times the month is assembled, wherever it is called from."""
    count = []
    real = solve_input.assemble

    def counted(*args, **kwargs):
        count.append(args[:2])
        return real(*args, **kwargs)

    from rosters import api, editing, review
    for module in (solve_input, api, editing, review):
        monkeypatch.setattr(module, "assemble", counted)
    return count


@pytest.fixture
def carla(client_of):
    return client_of("carla", Role.CNO, full_name="Carla CNO")


def read_once(client, unit, readings, django_assert_max_num_queries) -> dict:
    readings.clear()
    with django_assert_max_num_queries(QUERIES):
        answer = month_of(client, unit)
    assert len(readings) == 1, readings
    return answer


def test_a_draft_as_solved(mona, er, solver, readings, django_assert_max_num_queries):
    solved(mona, er, solver)
    answer = read_once(mona, er, readings, django_assert_max_num_queries)
    assert answer["draft"]["check"] is None and answer["draft"]["editor"]["nurses"]


def test_a_draft_edited_and_out_of_date_is_checked_on_the_same_reading(mona, er, solver, readings,
                                                                        django_assert_max_num_queries):
    solved(mona, er, solver)
    saved(mona, er, pin("Amal", 9, "N"))
    assert record_leave(mona, er, "Badr", BADR_OFF).status_code == 201

    answer = read_once(mona, er, readings, django_assert_max_num_queries)

    assert answer["draft"]["check"]["ok"] is True
    assert [(c["hospital_id"], c["what"]) for c in answer["draft"]["changes"]] == [("Amal", "pins"), ("Badr", "leave")]


def test_a_submitted_roster_tells_what_came_since_on_the_same_reading(mona, carla, er, solver, readings,
                                                                       django_assert_max_num_queries):
    solved(mona, er, solver)
    ok(submit(mona, er))
    assert record_leave(mona, er, "Badr", BADR_OFF).status_code == 201

    for client in (mona, carla):
        answer = read_once(client, er, readings, django_assert_max_num_queries)
        assert [(c["hospital_id"], c["what"]) for c in answer["submitted"]["new_since"]] == [("Badr", "leave")]


def test_an_approved_roster(mona, carla, er, solver, readings, django_assert_max_num_queries):
    solved(mona, er, solver)
    ok(submit(mona, er))
    ok(approve(carla, er))

    for client in (mona, carla):
        assert read_once(client, er, readings, django_assert_max_num_queries)["state"] == "approved"
