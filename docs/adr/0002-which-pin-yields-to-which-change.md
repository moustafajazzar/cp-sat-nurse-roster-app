---
status: accepted
---

# Which pin yields to which change

A cell that already holds a pin can be asked to change by four origins. Whether the pin is replaced or the change is
refused depends on both origins, and is decided in one table:

| Change asked by    | Manager's pin | Request's pin | Swap's pin | Cover's pin |
| ------------------ | ------------- | ------------- | ---------- | ----------- |
| Edit (and a clear) | replaces      | refused       | replaces   | replaces    |
| Swap               | replaces      | refused       | replaces   | replaces    |
| Cover              | replaces      | refused       | replaces   | cannot arise |
| Request's decision | replaces      | its own pin   | refused    | refused     |

Leave is not in the table: it ends every pin on its days, whatever the origin. A pin that is set aside still holds its
cell here, since it is not ended and may count again.

A nurse's new request is refused when she makes it on a cell her own swap's or cover's pin holds, since its decision would
be refused.

A request's pin is the one a manager's edit cannot touch. It is a promise made to one nurse, and it has a page of its own
where the promise is changed or cancelled and she is told. A swap's pin and a cover's pin have no such place: once the
swap is approved or the cover taken, nothing but the manager's edit, another swap or leave could change that cell, so
refusing the edit left the manager recording leave to correct a roster.

A request's decision is refused on a swap's pin and on a cover's pin so that it cannot undo a swap or a cover without a
word: the cell would change, the swap or the cover would still read as done, and nobody in it would be told why. A manager
who means to grant the request edits the cell first, which replaces that pin, and then decides the request.

## Considered options

- **Every non-manager pin refuses an edit.** How it stood. Rejected because "change it there" named a place that does not
  exist for a swap or a cover.
- **An edit replaces every pin.** Rejected: a request's promise would change with no word to the nurse, beside a page
  built to change it and tell her.
- **A request's decision replaces a cover's pin.** How it stood, by omission. Rejected: it silently undid a cover.
