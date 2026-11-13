"""Gmail live reads for the shared reader contract in `sources.py`.

Gmail has no broad listing; `live_readers.list_live` rejects it before access.
"""

from contextlib import contextmanager

from .core import FamiliekilderError


@contextmanager
def connect(root):
    from . import gmail as m

    client = m.Client(root)
    try:
        yield m, client
    finally:
        client.close()


def list_items(root, m, c, since, *, include_detail_hash=False):
    raise FamiliekilderError("Gmail støtter målrettet gmail_search, ikke brede listeoppslag")


def get_item(root, m, c, ref):
    return c.item(ref)


def download(root, m, c, item, attachment_id, reference, directory):
    return c.download(item, attachment_id, directory)
