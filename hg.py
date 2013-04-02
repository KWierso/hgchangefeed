#!/usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

DEFAULT_CHANGESETS = 1000
BATCH_SIZE = 500

from mercurial import demandimport;
demandimport.disable()

import os
import sys

project = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if project not in sys.path:
    sys.path.insert(0, project)
os.environ['DJANGO_SETTINGS_MODULE'] = 'hgchangefeed.settings'

from datetime import datetime

from mercurial.encoding import encoding
from mercurial import hg
from mercurial import error
import mercurial.ui

from django.db import transaction
from django.utils.tzinfo import FixedOffset

from website.models import *

class Options(object):
    def __init__(self, ui, repo, url = None):
        self.max_changesets = int(ui.config("hgchangefeed", "maxchangesets", default = DEFAULT_CHANGESETS))
        self.url = ui.config("hgchangefeed", "url", default = url)
        self.name = ui.config("hgchangefeed", "name")
        self.replace_stale = ui.configbool("hgchangefeed", "replacestale", False)

        if self.name is None:
            self.name = os.path.basename(repo.root)

def add_paths(ui, repository, files):
    root = Path(id = Path.next_id(), repository = repository, name = '', path = '', parent = None)
    paths = [root]
    parentlist = [root]
    path_count = 1

    pos = 0
    for file in files:
        ui.progress("indexing files", pos, item = file, total = len(files))

        while not file.startswith(parentlist[-1].path):
            parentlist.pop()

        remains = file[len(parentlist[-1].path):].split("/")
        if not remains[0]:
            remains.pop(0)

        while len(remains):
            name = remains[0]
            path = parentlist[-1].path
            if path:
                path = path + "/"
            path = path + remains[0]
            remains.pop(0)

            newpath = Path(id = Path.next_id(),
                           repository = repository,
                           name = name,
                           path = path,
                           parent = parentlist[-1],
                           is_dir = True if len(remains) else False)
            paths.append(newpath)
            path_count = path_count + 1

            if len(paths) >= BATCH_SIZE:
                Path.objects.bulk_create(paths)
                paths = []

            if len(remains):
                parentlist.append(newpath)

        pos = pos + 1

    if len(paths):
        Path.objects.bulk_create(paths)

    ui.progress("indexing files", None)
    ui.status("added %d files to database\n" % path_count)

def get_path(repository, path, is_dir = False):
    try:
        return Path.objects.get(repository = repository, path = path)
    except Path.DoesNotExist:
        parts = path.rsplit("/", 1)
        parent = get_path(repository, parts[0] if len(parts) > 1 else '', True)
        result = Path(id = Path.next_id(), repository = repository, path = path, name = parts[-1], parent = parent, is_dir = is_dir)
        result.save()
        return result

def get_author(author):
    result, created = Author.objects.get_or_create(author = unicode(author, encoding))
    return result

@transaction.commit_on_success()
def bulk_insert(changesets, changes, descendants):
    Changeset.objects.bulk_create(changesets)
    Change.objects.bulk_create(changes)
    DescendantChange.objects.bulk_create(descendants)

def add_changesets(ui, repo, options, repository, revisions):
    changesets = []
    changeset_count = 0
    changes = []
    descendants = []
    change_count = 0

    pos = 0
    for i in revisions:
        changectx = repo.changectx(i)
        ui.progress("indexing changesets", pos, changectx.hex(), total = len(revisions))
        pos = pos + 1

        tz = FixedOffset(-changectx.date()[1] / 60)
        date = datetime.fromtimestamp(changectx.date()[0], tz)

        try:
            changeset = Changeset.objects.get(repository = repository, hex = changectx.hex())
            if options.replace_stale:
                ui.warn("deleting stale information for changeset %s\n" % changeset)
                changeset.delete()
            else:
                continue
        except:
            pass

        changeset = Changeset(Changeset.next_id(),
                              repository = repository,
                              rev = changectx.rev(),
                              hex = changectx.hex(),
                              author = get_author(changectx.user()),
                              date = date,
                              tz = -changectx.date()[1] / 60,
                              description = unicode(changectx.description(), encoding))

        parents = changectx.parents()

        added = False
        for file in changectx.files():
            path = get_path(repository, file)

            type = "M"

            if not file in changectx:
                if all([file in c for c in parents]):
                    type = "R"
                else:
                    continue
            else:
                filectx = changectx[file]
                if not any([file in c for c in parents]):
                    type = "A"
                elif all([filectx.cmp(c[file]) for c in parents]):
                    type = "M"
                else:
                    continue

            if not added:
                changesets.append(changeset)
                changeset_count = changeset_count + 1
                added = True

            change = Change(id = Change.next_id(), changeset = changeset, path = path, type = type)
            changes.append(change)
            change_count = change_count + 1

            depth = 0
            while path is not None:
                descendants.append(DescendantChange(change = change, path = path, depth = depth))
                path = path.parent
                depth = depth + 1

        if (len(changesets) + len(changes) + len(descendants)) >= BATCH_SIZE:
            bulk_insert(changesets, changes, descendants)
            changesets = []
            changes = []
            descendants = []

    bulk_insert(changesets, changes, descendants)

    ui.progress("indexing changesets", None)
    ui.status("added %d changesets with changes to %d files to database\n" % (changeset_count, change_count))

@transaction.commit_manually()
def add_repository(ui, repo, options):
    # If adding paths fails then we want to roll back the repository info too
    try:
        repository = Repository(localpath = repo.root, url = options.url, name = options.name)
        repository.save()

        tip = repo.changectx("tip")
        add_paths(ui, repository, [f for f in tip])
    except:
        transaction.rollback()
        raise
    else:
        transaction.commit()

    # New repository, attempt to add the maximum number of changesets
    rev = tip.rev() - options.max_changesets
    add_changesets(ui, repo, options, repository, xrange(tip.rev(), rev, -1))

def expire_changesets(ui, repo, options, repository):
    oldsets = Changeset.objects.filter(repository = repository)[options.max_changesets:]
    pos = 0
    for changeset in oldsets:
        ui.progress("expiring changesets", pos, changeset.hex, total = len(oldsets))
        changeset.delete()
        pos = pos + 1
    ui.progress("expiring changesets", None)
    if len(oldsets) > 0:
        ui.status("expired %d changesets from database\n" % len(oldsets))

def pretxnchangegroup(ui, repo, node, **kwargs):
    options = Options(ui, repo, kwargs["url"])

    try:
        repository = Repository.objects.get(localpath = repo.root)
        if repository.url is None and options.url is not None:
            repository.url = options.url
            repository.save()

        # Existing repository, only add new changesets
        # All changesets from node to "tip" inclusive are part of this push.
        tip = repo.changectx("tip")
        rev = max(tip.rev() - options.max_changesets, repo.changectx(node).rev())
        add_changesets(ui, repo, options, repository, xrange(rev, tip.rev() + 1))

        expire_changesets(ui, repo, options, repository)

    except Repository.DoesNotExist:
        add_repository(ui, repo, options)

    return False

def init(ui, repo, options):
    try:
        repository = Repository.objects.get(localpath = repo.root)
        raise Exception("Repository already exists in the database")
    except Repository.DoesNotExist:
        add_repository(ui, repo, options)

@transaction.commit_on_success()
def fixrevs(ui, repo, options):
    try:
        repository = Repository.objects.get(localpath = repo.root)

        update_count = 0
        count = 0
        changesets = Changeset.objects.filter(repository = repository)
        for changeset in changesets:
            ui.progress("updating changesets", count, changeset.hex, total = changesets.count())
            changectx = repo.changectx(changeset.hex)
            if changectx.rev() != changeset.rev:
                changeset.rev = changectx.rev()
                changeset.save()
                update_count = update_count + 1
            count = count + 1
        ui.progress("updating changesets", None)
        ui.status("corrected %d changesets\n" % update_count)

    except Repository.DoesNotExist:
        raise Exception("Repository doesn't exist in the database")

def update(ui, repo, options):
    try:
        repository = Repository.objects.get(localpath = repo.root)
        tip = repo.changectx("tip")
        rev = tip.rev() - options.max_changesets
        add_changesets(ui, repo, options, repository, xrange(tip.rev(), rev, -1))
    except Repository.DoesNotExist:
        raise Exception("Repository doesn't exist in the database")

def reset(ui, repo, options):
    try:
        repository = Repository.objects.get(localpath = repo.root)
        delete(ui, repo, options)

        if options.onlychangesets:
            tip = repo.changectx("tip")
            rev = tip.rev() - options.max_changesets
            add_changesets(ui, repo, options, repository, xrange(tip.rev(), rev, -1))
        else:
            init(ui, repo, options)
    except Repository.DoesNotExist:
        raise Exception("Repository doesn't exist in the database")

@transaction.commit_on_success()
def delete(ui, repo, options):
    try:
        repository = Repository.objects.get(localpath = repo.root)

        from django.conf import settings
        count = 0
        changesets = Changeset.objects.filter(repository = repository)
        for c in changesets:
            ui.progress("deleting changesets", count, c.hex, total = len(changesets))
            c.delete()
            count = count + 1
        ui.progress("deleting changesets", None)
        ui.status("deleted changesets\n")

        if options.onlychangesets:
            return

        count = 0
        path_count = Path.objects.filter(repository = repository).count()
        remains = path_count
        while remains > 0:
            paths = Path.objects.filter(repository = repository).order_by("-path")[:BATCH_SIZE]
            for p in paths:
                ui.progress("deleting paths", count, p, total = path_count)
                p.delete()
                count = count + 1
            remains = Path.objects.filter(repository = repository).count()
        ui.progress("deleting paths", None)
        ui.status("deleted paths\n")

        repository.delete()
        ui.status("deleted repository\n")

    except Repository.DoesNotExist:
        raise Exception("Repository doesn't exist in the database")

def cmdline():
    import argparse

    ui = mercurial.ui.ui()
    try:
        repo = hg.repository(ui, os.getcwd())
        ui = repo.ui
        options = Options(ui, repo)

        parser = argparse.ArgumentParser(description='Bootstrap hgchangefeed database for a mercurial repository.')
        parser.add_argument("command", metavar = "cmd", type = str, choices = ["init", "update", "fixrevs", "reset", "delete"],
                            help = "Command to run (init|update|reset|delete)")
        parser.add_argument("--changesets", dest = "onlychangesets", action = 'store_const',
                            const = True, default = False,
                            help = "Only delete/reset changesets")
        parser.add_argument("--maxchangesets", dest = "max_changesets", type = int,
                            default = argparse.SUPPRESS,
                            help = "The maximum changesets to keep in the database")
        parser.add_argument("--replacestale", dest = "replace_stale", action = 'store_const',
                            const = True,
                            help = "Replace any stale changesets when updating")
        parser.parse_args(namespace = options)

        if options.command == "init":
            init(ui, repo, options)
        elif options.command == "fixrevs":
            fixrevs(ui, repo, options)
        elif options.command == "update":
            update(ui, repo, options)
        elif options.command == "reset":
            reset(ui, repo, options)
        elif options.command == "delete":
            delete(ui, repo, options)

    except error.RepoError:
        ui.warn("%s is not a mercurial repository.\n" % os.getcwd())

if __name__ == "__main__":
    cmdline()
