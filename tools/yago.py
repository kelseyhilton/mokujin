#!/usr/bin/env python
# coding: utf-8

# Copyright (C) USC Information Sciences Institute
# Author: Vladimir M. Zaytsev <zaytsev@usc.edu>
# URL: <http://nlg.isi.edu/>
# For more information, see README.md
# For license information, see LICENSE

import re
import os
import sys
import pickle
import leveldb
import logging
import sqlite3
import argparse
import itertools


ARG_NONE = 0x0
ARG_EMPTY = 0x1


class NodeType(object):
    WORDNET = 0x1
    OWL = 0x2
    WIKI_INSTANCE = 0x3
    WIKI_CATEGORY = 0x4
    YAGO = 0x5

class ConceptRelation(object):
    ConceptuallyRelatedTo = 0x1
    DerivedFrom = 0x2
    Synonym = 0x3

class Output(object):

    def __init__(self, debug, out_file):
        self.out_file = out_file
        self.debug = debug
        self.out_files = dict()
        if debug:
            if not os.path.exists("debug"):
                os.makedirs("debug")

    def get_file(self, rel_class=None, compound=False):
        if self.debug:
            if compound:
                fl = self.out_files.get("compound", None)
                if fl is None:
                    fl = open("debug/compound.txt", "w")
                    self.out_files["compound"] = fl
                    return fl
                return fl
            fl = self.out_files.get(rel_class, None)
            if fl is None:
                fl = open("debug/%s.txt" % rel_class, "w")
                self.out_files[rel_class] = fl
                return fl
            return fl
        return self.out_file

    def close(self):
        for fl in self.out_files.itervalues():
            fl.close()


class YagoEntry(object):
    word_re = re.compile(ur"\b[^\W\d_]+\b", re.UNICODE)

    def __init__(self, wn_node, rdf_label, lang=None):
        self.node = wn_node
        self.label = rdf_label.lower()
        self.lang = lang

    @staticmethod
    def instance_size(inst_node):
        return len(YagoEntry.word_re.findall(inst_node))

    @staticmethod
    def from_tsv_line(tsv_line, simplify=True):
        tsv_line = tsv_line.decode("utf-8")
        row = tsv_line.split("\t")
        if simplify:
            wn_node = row[1]
            rdf_label = row[3].lower()
            label_parts = rdf_label.split("@")
            lang = label_parts[-1]
            rdf_label = "".join(label_parts[0:(len(label_parts) - 1)])
            rdf_label = rdf_label[1:(len(rdf_label) - 1)]
            return YagoEntry(wn_node, rdf_label, lang)
        else:
            wn_node = row[1]
            rdf_label = row[3].lower()
            label_parts = rdf_label.split("@")
            lang = label_parts[-1]
            return YagoEntry(wn_node, rdf_label, lang)

    @staticmethod
    def is_class(node):
        if node.startswith("<wordnet"):
            return True
        return False

    @staticmethod
    def extract_transition(tsv_line):
        line = tsv_line.decode("utf-8")
        row = line.split("\t")
        return row[1], row[3]

    def __repr__(self):
        repr_str = u"<YagoEntry(%s, %s, \"%s\")>" % (self.lang, self.node, self.label)
        return repr_str.encode("utf-8")


class YagoDict(object):
    SQL_CREATE_TABLE_STATEMENTS = (
        """
        CREATE TABLE IF NOT EXISTS yago_node (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label VARCHAR(128) NOT NULL,
            node VARCHAR(128) NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS yago_cpnd (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            part VARCHAR(128) NOT NULL,
            node INTEGER NOT NULL,
            UNIQUE (part, node)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS yago_taxn (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ins VARCHAR(64) NOT NULL,
            cls VARCHAR(64) NOT NULL,
            UNIQUE (ins, cls)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS yago_hrch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            child VARCHAR(64) NOT NULL,
            parent VARCHAR(64) NOT NULL,
            UNIQUE (parent, child)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS conceptnet (
            rel INT2 NOT NULL,
            concept VARCHAR(64) NOT NULL,
            form VARCHAR(64) NOT NULL,
            pos VARCHAR(1) NOT NULL DEFAULT '?',
            PRIMARY KEY (rel, concept, form, pos)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS names (
            name VARCHAR(128),
            PRIMARY KEY (name)
        );
        """,

        "CREATE UNIQUE INDEX IF NOT EXISTS `yago_node_id_idx` ON `yago_node` (`id` ASC);",
        "CREATE INDEX IF NOT EXISTS `yago_node_label_idx` ON `yago_node` (`label` ASC);",
        "CREATE INDEX IF NOT EXISTS `yago_node_node_idx` ON `yago_node` (`node` ASC);",

        "CREATE UNIQUE INDEX IF NOT EXISTS `yago_cpnd_id_idx` ON `yago_cpnd` (`id` ASC);",
        "CREATE INDEX IF NOT EXISTS `yago_cpnd_part_idx` ON `yago_cpnd` (`part` ASC);",
        "CREATE INDEX IF NOT EXISTS `yago_cpnd_node_idx` ON `yago_cpnd` (`node` ASC);",

        "CREATE UNIQUE INDEX IF NOT EXISTS `yago_taxn_id_idx` ON `yago_taxn` (`id` ASC);",
        "CREATE INDEX IF NOT EXISTS `yago_taxn_ins_idx` ON `yago_taxn` (`ins` ASC);",
        "CREATE INDEX IF NOT EXISTS `yago_taxn_cls_idx` ON `yago_taxn` (`cls` ASC);",

        "CREATE INDEX IF NOT EXISTS `yago_hrch_child_idx` ON `yago_hrch` (`child` ASC);",
        "CREATE INDEX IF NOT EXISTS `yago_hrch_parent_idx` ON `yago_hrch` (`parent` ASC);",

        "CREATE INDEX IF NOT EXISTS `conceptnet_concept_idx` ON `conceptnet` (`concept` ASC);",
        "CREATE INDEX IF NOT EXISTS `conceptnet_form_idx` ON `conceptnet` (`form` ASC);",

        "CREATE INDEX IF NOT EXISTS `names_name_idx` ON `names` (`name` ASC);",
    )

    def __init__(self, yago_dir, db_dir):
        self.kvs_place = "%s/kvs.db" % db_dir
        self.idx_place = "%s/idx.db" % db_dir
        self.sql_place = "%s/sql.db" % db_dir
        self.txn_place = "%s/txn.db" % db_dir

        self.sql = sqlite3.connect(self.sql_place)
        self.kvs = leveldb.LevelDB(self.kvs_place)
        self.idx = leveldb.LevelDB(self.idx_place)
        self.txn = leveldb.LevelDB(self.txn_place)

        self.sql_r_cursor = self.sql.cursor()
        self.sql_w_cursor = self.sql.cursor()

        self.kvs_batch = leveldb.WriteBatch()
        self.idx_batch = leveldb.WriteBatch()

        self.yago_dir = yago_dir
        self.db_dir = db_dir
        self.__kvs_counter = 0

        for statement in YagoDict.SQL_CREATE_TABLE_STATEMENTS:
            self.sql_w_cursor.execute(statement)
        self.sql.commit()

    @staticmethod
    def create(yago_dir, db_dir, lang=None):
        yago_classes_fl = "%s/yagoMultilingualClassLabels.tsv" % yago_dir
        yago_instances_fl = "%s/yagoMultilingualInstanceLabels.tsv" % yago_dir

        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
        yago = YagoDict(yago_dir, db_dir)

        with open(yago_classes_fl, "rb") as classes, open(yago_instances_fl, "rb") as instances:
            for line in itertools.chain(classes, instances):
                entry = YagoEntry.from_tsv_line(line)
                if lang is not None and entry.lang == lang:
                    yago.sql_insert_entry(entry)
                elif lang is None:
                    yago.sql_insert_entry(entry)

        with open(yago_transitions_fl, "rb") as transitions:
            for line in transitions:
                node_a, node_b = YagoEntry.extract_transition(line)
                yago.sql_insert_txn_transition(node_a, node_b)

        yago.sql.commit()
        return yago

    def create_txn(self):
        yago_transitions_fl = "%s/yagoTransitiveType.tsv" % self.yago_dir
        with open(yago_transitions_fl, "rb") as transitions:
            for line in transitions:
                node_a, node_b = YagoEntry.extract_transition(line)
                self.sql_insert_txn_transition(node_a, node_b)
        self.sql.commit()

    def create_hrc(self):
        yago_taxonomy_fl = "%s/yagoTaxonomy.tsv" % self.yago_dir
        with open(yago_taxonomy_fl, "rb") as relations:
            for line in relations:
                row = line.decode("utf-8").split("\t")
                child, parent = row[1], row[3]
                self.sql_insert_txn_relation(child, parent)
        self.sql.commit()

    def create_names(self, lang="ru"):
        names_list_fl = "%s/names_%s.txt" % (self.yago_dir, lang.upper())
        with open(names_list_fl, "rb") as names:
            for line in names:
                name = line.decode("utf-8").split("\n")[0]
                self.sql_insert_name(name)
        self.sql.commit()

    def create_conceptnet(self, lang="ru"):
        conceptnet_fl = "%s/conceptnet5_filtered_%s.csv" % (self.yago_dir, lang.upper())
        with open(conceptnet_fl, "rb") as conceptnet_rels:
            for line in conceptnet_rels:
                rel_name, form, concept = line.decode("utf-8").split("\t")
                self.sql_insert_concept(rel_name, form, concept)
        self.sql.commit()

    def create_kvs_from_sql(self):
        prev = None
        node_set = None
        for label, node in self.sql_r_cursor.execute("SELECT label,node FROM yago_node ORDER BY label;"):
            if label != prev:
                if node_set is not None and len(node_set) > 0:
                    self.kvs_insert_entryset(prev.encode("utf-8"), node_set)
                prev = label
                node_set = set()
            node_set.add(node)
        if node_set is not None and len(node_set) > 0:
            self.kvs_insert_entryset(prev.encode("utf-8"), node_set)
        self.kvs.Write(self.kvs_batch, sync=True)

    def create_idx_from_sql(self):
        word_re = YagoEntry.word_re
        for label, node in self.sql_r_cursor.execute("SELECT label,node FROM yago_node ORDER BY label;"):
            label_parts = word_re.findall(label)
            for part in label_parts:
                self.sql_insert_part(part, node)
        self.sql.commit()
        prev = None
        node_set = None
        for part, node in self.sql_r_cursor.execute("SELECT part,node FROM yago_cpnd ORDER BY part;"):
            if part != prev:
                if node_set is not None and len(node_set) > 0:
                    self.index_part(prev.encode("utf-8"), node_set)
                prev = part
                node_set = set()
            node_set.add(node)
        if node_set is not None and len(node_set) > 0:
            self.index_part(prev.encode("utf-8"), node_set)
        self.idx.Write(self.idx_batch, sync=True)

    def create_txn_from_sql(self):
        print "CREATE TAXONOMY"

    def sql_insert_concept(self, rel_name, form, concept):
        concept_spl = concept.split("/")
        concept = concept_spl[0].lower()
        pos = concept_spl[-1] if len(concept_spl) > 1 else "?"
        form = form.lower()
        if rel_name == "DerivedFrom":
            values = (ConceptRelation.DerivedFrom, concept, form, pos)
        elif rel_name == "ConceptuallyRelatedTo":
            values = (ConceptRelation.ConceptuallyRelatedTo, concept, form, pos)
        elif rel_name == "Synonym":
            values = (ConceptRelation.Synonym, concept, form, pos)
        else:
            logging.msg("ERROR: unknown conceptnet relation type")
        sql_insert = u"INSERT INTO conceptnet (rel,concept,form,pos) VALUES (?,?,?,?);"
        try:
            self.sql_w_cursor.execute(sql_insert, values)
        except sqlite3.Error:
            pass

    def sql_insert_entry(self, entry):
        sql_insert = u"INSERT INTO yago_node (label, node) VALUES (?,?);"
        values = (entry.label, entry.node)
        try:
            self.sql_w_cursor.execute(sql_insert, values)
        except sqlite3.Error:
            pass

    def sql_insert_txn_transition(self, inst_node, class_node):
        sql_insert = u"INSERT INTO yago_taxn (ins, cls) VALUES (?,?);"
        values = (inst_node, class_node)
        try:
            self.sql_w_cursor.execute(sql_insert, values)
        except sqlite3.Error:
            pass

    def sql_insert_txn_relation(self, child, parent):
        sql_insert = u"INSERT INTO yago_hrch (child, parent) VALUES (?,?);"
        values = (child, parent)
        try:
            self.sql_w_cursor.execute(sql_insert, values)
        except sqlite3.Error:
            pass

    def sql_insert_name(self, name):
        sql_insert = u"INSERT INTO names (name) VALUES (?);"
        try:
            self.sql_w_cursor.execute(sql_insert, (name, ))
        except sqlite3.Error:
            pass


    def sql_insert_part(self, part, node):
        try:
            sql_insert = u"INSERT INTO yago_cpnd (part, node) VALUES (?,?);"
            self.sql_w_cursor.execute(sql_insert, (part, node))
        except sqlite3.Error:
            pass

    def kvs_insert_entryset(self, label, node_set):
        self.kvs_batch.Put(label, pickle.dumps(node_set, protocol=pickle.HIGHEST_PROTOCOL))
        self.__kvs_counter += 1
        if self.__kvs_counter % 100000 == 0:
            self.kvs.Write(self.kvs_batch, sync=True)

    def index_part(self, part, node_set):
        self.idx_batch.Put(part, pickle.dumps(node_set, protocol=pickle.HIGHEST_PROTOCOL))
        self.__kvs_counter += 1
        if self.__kvs_counter % 100000 == 0:
            self.idx.Write(self.kvs_batch, sync=True)

    def find_concept(self, form, rel=None):
        if rel is None:
            values = (form, )
            sql_statement = "SELECT concept FROM conceptnet WHERE form=?;"
        else:
            values = (form, rel, )
            sql_statement = "SELECT concept FROM conceptnet WHERE form=? AND rel=?;"
        concepts = [row[0] for row in self.sql_r_cursor.execute(sql_statement, values)]
        if len(concepts) > 0:
            return set(concepts)
        return None

    def find_class(self, instance):
        sql_statement = "SELECT cls FROM yago_taxn WHERE ins=?;"
        classes = [row[0] for row in self.sql_r_cursor.execute(sql_statement, (instance, ))]
        if len(classes) > 0:
            return set(classes)
        return None

    def find_all_classes(self, child):
        classes = [child]
        sql_statement = "SELECT parent FROM yago_hrch WHERE child=?;"
        new_classes = [row[0] for row in self.sql_r_cursor.execute(sql_statement, (child, ))]
        while len(new_classes) > 0:
            classes.extend(new_classes)
            children = new_classes
            new_classes = []
            for ch in children:
                new_classes.extend([row[0] for row in self.sql_r_cursor.execute(sql_statement, (ch, ))])
        return set(classes)

    def is_name(self, name):
        sql_statement = "SELECT name FROM names WHERE name=?;"
        names = self.sql_r_cursor.execute(sql_statement, (name, ))
        return len(list(names)) > 0

    def sql_map_lemma(self, lemma):
        sql_statement = "SELECT node FROM yago_node WHERE label=?;"
        nodes = [row[0] for row in self.sql_r_cursor.execute(sql_statement, (lemma, ))]
        if len(nodes) > 0:
            return set(nodes)
        return None

    def kvs_map_lemma(self, lemma):
        try:
            db_value = self.kvs.Get(lemma)
            return pickle.loads(db_value)
        except KeyError:
            return None

    def idx_map_part(self, part):
        try:
            db_value = self.idx.Get(part)
            return pickle.loads(db_value)
        except KeyError:
            return None

    def idx_map_compound(self, compound):
        node_sets = [self.idx_map_part(part.encode("utf-8")) for part in compound]
        intersection = node_sets[0]
        for ns in node_sets:
            if ns is not None:
                intersection &= ns
            else:
                return set()
        return intersection

    def expand_instances(self, node_set):
        new_nodes = set()
        for node in node_set:
            if not YagoEntry.is_class(node):
                found_nodes = self.find_class(node)
                if found_nodes is not None:
                    for foud_node in found_nodes:
                        new_nodes.add(foud_node)
            else:
                new_nodes.add(node)
        return new_nodes

    def find_compound2(self, lemmas, min_threshold=1, init_len=1, max_len=3, prefer_classes=True):
        if len(lemmas) == 0:
            return None
        best_nodes = None
        best_len = 0xFFFFFF
        for comb_len in xrange(init_len, min(len(lemmas) + 1, max_len + 1)):
            combs = list(itertools.combinations(lemmas, comb_len))
            for comb in combs:
                nodes = self.idx_map_compound(comb)
                if min_threshold <= len(nodes) <= best_len:
                    best_nodes = nodes
                    best_len = len(best_nodes)
        if prefer_classes and best_nodes is not None:
            classes_found = False
            for node in best_nodes:
                if YagoEntry.is_class(node):
                    classes_found = True
                    break
            if classes_found:
                best_nodes = filter(lambda node: YagoEntry.is_class(node), best_nodes)
            else:
                best_nodes = [min(best_nodes, key=lambda node: len(node.split("_")))]
        return best_nodes

    def find_compound(self, lemmas):
        if len(lemmas) == 0:
            return None
        initial_sets = [self.idx_map_compound([lemma]) for lemma in lemmas]
        initial_sets = filter(lambda s: len(s) > 0, initial_sets)
        if len(initial_sets) == 0:
            return None
        initial_sets = [self.expand_instances(nset) for nset in initial_sets]
        intersection = initial_sets[0]
        for i in xrange(1, len(initial_sets)):
            intersection = intersection & initial_sets[i]
        if len(intersection) > 0:
            return intersection
        return set([])



def parse_triple_row(csv_row):
    rel_name = csv_row[0]
    freq = csv_row[-1]
    args = []
    for arg in csv_row[1:(len(csv_row)-1)]:
        if arg == "<NONE>":
            args.append(ARG_NONE)
        elif arg == "<->":
            args.append(ARG_EMPTY)
        else:
            arg = arg.split("-")
            pos = arg[-1]
            lemmas = "".join(arg[0:(len(arg) - 1)])
            args.append((lemmas, pos))
    return rel_name, args, freq


def process_triple(yago, out_file, rel_name, freq, test=True, *args):

    if test:
        for arg in args:
            if arg is ARG_NONE:
                continue
            elif arg is ARG_EMPTY:
                continue
            else:
                lemmas, pos = arg
                lemmas_set = lemmas.split("&&")
                if pos == "NN" and len(lemmas_set) > 1:
                    nodes = yago.find_compound(lemmas_set)
                    if nodes is not None and len(nodes) > 0:
                        out_file.write(lemmas.encode("utf-8"))
                        out_file.write("\t")
                        out_file.write(("{%s}" % ";".join(nodes)).encode("utf-8"))
                        if len(nodes) == 1 and not YagoEntry.is_class(list(nodes)[0]):
                            out_file.write(" => ")
                            out_file.write("\n")
                            classes = yago.find_class(list(nodes)[0])
                            for cl in classes:
                                out_file.write("\t\t")
                                out_file.write(cl.encode("utf-8"))
                                out_file.write("\n")
                        out_file.write("\n")
    else:
        out_file.write(rel_name)
        out_file.write(",")
        for arg in args:
            if arg is ARG_NONE:
                out_file.write("<NONE>,")
            elif arg is ARG_EMPTY:
                out_file.write("<->,")
            else:
                lemmas, pos = arg
                if pos == "NN":
                    lemmas_set = lemmas.split("&&")
                    if len(lemmas_set) == 1:
                        nodes = yago.kvs_map_lemma(lemmas_set[0].encode("utf-8"))
                    else:
                        nodes = yago.find_compound(lemmas_set)
                    if nodes is None or len(nodes) == 0:
                        lemma_node_sets = "{}"
                    else:
                        lemma_node_sets = "{%s}" % ";".join(nodes)
                    out_file.write(lemma_node_sets.encode("utf-8"))
                    out_file.write("/")
                    out_file.write(lemmas.encode("utf-8"))
                    out_file.write("-")
                    out_file.write(pos)
                    out_file.write(",")
                else:
                    out_file.write(lemmas.encode("utf-8"))
                    out_file.write("-")
                    out_file.write(pos)
                    out_file.write(",")
        out_file.write(freq)


def map_triples(yago, triples_file, out):
    for line in triples_file:
        line = line.decode("utf-8")
        row = line.split(", ")
        rel_name, args, freq = parse_triple_row(row)
        out_file = out.get_file(rel_name, any([arg != ARG_NONE and
                                               arg != ARG_EMPTY and
                                               len(arg[0].split("&&")) > 1
                                               for arg in args]))
        process_triple(yago, out_file, rel_name, freq, True, *args)




if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("-y", "--yago", default=None, type=str, help="A path to the directory containing "
                                                                     "yagoMultilingualClassLabels.tsv and "
                                                                     "yagoMultilingualInstanceLabels.tsv files")
    parser.add_argument("-d", "--dbdir", default="yago.ldb", type=str, help="A path to the temp database directory"
                                                                            " which will be created")
    parser.add_argument("-c", "--createdb", default=1, type=int, choices=(0, 1), help="Create a temp db for YAGO if it "
                                                                                      "does not exist")
    parser.add_argument("-l", "--lang", default=None, type=str, help="A lang of the input data")
    parser.add_argument("-i", "--ifile", default=None, type=str, help="A path to the input csv file with the triples")
    parser.add_argument("-o", "--ofile", default=None, type=str, help="A path to the result file")
    parser.add_argument("-t", "--debug", default=0, type=int, choices=(0, 1), help="Enables debug mode")

    args = parser.parse_args()

    in_file = file(args.ifile, "r") if args.ifile is not None else sys.stdin
    out_file = file(args.ofile, "w") if args.ofile is not None else sys.stdout
    yago_dir = args.yago
    db_dir = args.dbdir
    lang = args.lang
    create_db = args.createdb
    debug = args.debug

    if debug:
        logging.basicConfig(level=logging.DEBUG)

    if create_db == 1: pass
        # logging.info("CREATING A TEMP DB FOR YAGO(%s): %s" % (yago_dir, db_dir))
        # logging.info("CREATING MAIN SQL STORAGE")
        # yago = YagoDict.create(yago_dir, db_dir, lang)
        # logging.info("CREATING KV STORAGE")
        # yago.create_kvs_from_sql()
        # logging.info("CREATING INVERTED INDEX")
        # yago.create_idx_from_sql()
        # logging.info("CREATING TAXONOMY INDEX")
        # yago.create_txn_from_sql()

        # yago.create_conceptnet()
        # yago.create_hrc()
        # yago.create_txn_from_sql()
        # yago.create_names()
        # yago.create_names()
    else:
        logging.info("LOADING TEMP DB: %s" % db_dir)
        yago = YagoDict(yago_dir, db_dir)


    test_2list = [
        [u"академик", u"сахаров"],
        [u"алексей", u"андреев", u"архипов"],
        [u"гдов", u"писатель"],
        [u"геворг", u"повар"],
        [u"россия", u"партия"],
        [u"россия", u"партия", u"единая"],
        [u"глава", u"сергей", u"иванов"],
        [u"владимир", u"путин"],
        [u"президент", u"владимир", u"путин", u"владимирович"],
        [u"президент", u"дмитрий", u"медведев"],
        [u"дмитрий", u"медведев"],
        [u"дмитрий", u"медведев", u"анатольевич"],
        [u"коэн", u"морис"],
        [u"глава", u"аранович", u"ицхак"],
        [u"билл", u"гейтс"],
        [u"опозиционер", u"алексей", u"навальный"],
    ]

    for comp in test_2list:
        print "COMPOUND:"
        for l in comp:
            print l.encode("utf-8")
        # print "ALL NODES", yago.find_compound(comp, prefer_classes=False)
        # print "WHEN PREFER SHORT CLASSES", yago.find_compound(comp)
        print yago.find_compound(comp)
        print

    # test3 = [
    #     u"дарственная",
    #     u"нырять",
    #     u"лекционный",
    #     u"президентский",
    #     u"потрясти",
    # ]

    # for form in test3:
    #     print form.upper().encode("UTF-8")
    #     concepts = yago.find_concept(form)
    #     if concepts is not None:
    #         for cn in concepts:
    #             print cn.encode("utf-8")
    #     else:
    #         print "{}"
    #     print

    # print yago.find_all_classes("<wikicategory_Presidents_of_the_Montana_Senate>")

    # test = [
    #     u"владимир",
    #     u"зайцев",
    #     u"сахаров",
    #     u"вова",
    #     u"володя",
    #     u"тарас",
    #     u"лена",
    #     u"путин",
    #     u"алексей",
    #     u"иванова",
    #     u"алеша",
    #     u"николай",
    #     u"коля",
    #     u"лодка",
    #     u"стол",
    #     u"вода",
    #     u"книга",
    # ]

    # for name in test:
    #     print name.encode("utf-8"), yago.is_name(name)

    # print yago.expand_instances(set(["<Tim_Story>"]))

    exit(0)

    out = Output(debug, out_file)

    logging.info("MAPPING TRIPLES FROM %s TO %s" % (in_file, out_file))

    map_triples(yago, in_file, out)

    out.close()

    logging.info("DONE")