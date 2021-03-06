#!/usr/bin/env python

import os
import sys
import json
import h5py
import time
import psutil
import pickle
import logging
import argparse
import traceback
import numpy as np
import pandas as pd
from tqdm import tqdm
import concurrent.futures
from concurrent import futures
from collections import defaultdict

from Bio import SeqIO

if __name__ != '__main__':
    from ._version import __version__
import inStrain.profileUtilities
import inStrain.SNVprofile
import inStrain.controller

def main(args):
    '''
    The main controller of the program
    '''
    # Parse and validate arguments
    RCprof, names, Sprofiles, scaffolds_to_compare, outbase, s2l = parse_validate(args)
    vargs = vars(args)

    # The main for greedy clustering
    greedy = vargs.get('greedy_clustering', False)
    if greedy:
        print("Greedy clustering not available at the moment; sorry")
        return
        greedy_main(RCprof, names, Sprofiles, scaffolds_to_compare, s2l, **vargs)
        return

    # Compare scaffolds
    Cdb, Mdb, scaff2pair2mm2cov = compare_scaffolds(names, Sprofiles, scaffolds_to_compare, s2l, **vargs)

    # Store results
    outbase = RCprof.get_location('output') + os.path.basename(RCprof.get('location')) + '_'

    RCprof.store('comparisonsTable', Cdb, 'pandas', 'Comparisons between the requested IS objects')
    RCprof.store('scaffold2length', s2l, 'dictionary', 'Scaffold to length')
    Cdb = RCprof.get_nonredundant_RC_table()
    Cdb.to_csv(outbase + 'comparisonsTable.tsv', index=False, sep='\t')

    # Store scaff2pair2mm2SNPs
    if args.store_mismatch_locations:
        RCprof.store('pairwise_SNP_locations', Mdb, 'pandas', 'A dataframe of scaffold, IS pair, mm level, SNP locations')

    # Store scaff2pair2mm2cov
    if args.store_coverage_overlap:
        RCprof.store('scaff2pair2mm2cov', scaff2pair2mm2cov, 'special', 'A dictionary of scaffold -> IS pair -> mm level -> positions with coverage overlap')

def greedy_main(RCprof, names, Sprofiles, scaffolds_to_compare, s2l, **kwargs):
    '''
    Perform greedy clustering instead of all-vs-all comparisons
    '''
    g_ani = kwargs.get('g_ani', 0.99)
    g_cov = kwargs.get('g_cov', 0.5)
    g_mm = kwargs.get('g_mm', 100)

    # Set up the clustering
    name2cluster = {}
    cluster2rep = {}
    name2Sprofile = {n:s for n, s in zip(names, Sprofiles)}

    # Set up the initial cluster
    clusters = 0
    name2cluster[names[0]] = clusters
    cluster2rep[clusters] = names[0]
    clusters += 1

    # Make sure the names are unique
    assert len(names) == len(set(names)), "IS objects do not have unique names!"

    # Figure out the total bin length
    if kwargs.get('scaffolds', None) != None:
        scaffolds = inStrain.controller.load_scaff_list(kwargs.get('scaffolds', None))
        BIN_LENGTH = sum([s2l[s] for s in scaffolds])
    else:
        BIN_LENGTH = sum([l for s, l in s2l.items()])
    logging.info("BIN_LENGTH is {0}".format(BIN_LENGTH))

    # Do the iterations
    ndbs = []
    ddbs = []
    for name, IS in zip(names[1:], Sprofiles[1:]):

        # Get a list of Sprofiles to compare to
        To_compare = []
        compare_names = []
        for i in range(0, clusters):
            To_compare.append(name2Sprofile[cluster2rep[i]])
            compare_names.append(cluster2rep[i])

        # Get the distance matrix
        Ddb, Ndb = compare_Sprofiles_wrapper(IS, To_compare, name, compare_names, scaffolds_to_compare, s2l, BIN_LENGTH, **kwargs)
        ndbs.append(Ndb)
        ddbs.append(Ddb)

        # Adjust the clusters
        found = False
        for i, row in Ddb.iterrows():
            if (row['popANI'] >= g_ani) & (row['cov'] >= g_cov):
                found = True
                logging.debug("{0} is in the same cluster as {1}".format(name, row['name2']))
                name2cluster[name] = name2cluster[row['name2']]
                break

        if not found:
            logging.debug("{0} is a new cluster".format(name))
            name2cluster[name] = clusters
            cluster2rep[clusters] = name
            clusters += 1

    # Make the output
    Cdb = pd.DataFrame(list(name2cluster.items()), columns=['name', 'cluster'])
    Ndb = pd.concat(ndbs)
    Ddb = pd.concat(ddbs)

    # Store results
    outbase = RCprof.get_location('output') + os.path.basename(RCprof.get('location')) + '_'

    RCprof.store('greedy_clusters', Cdb, 'pandas', 'Cluster affiliations of the requested IS objects')
    RCprof.store('scaffold2length', s2l, 'dictionary', 'Scaffold to length')
    RCprof.store('comparisonsTable_greedy', Ndb, 'pandas', 'Comparisons between the requested IS objects done from a greedy clustering')
    RCprof.store('parsed_comparisonsTable_greedy', Ddb, 'pandas', 'Parsed comparisons between the requested IS objects done from a greedy clustering')

    Cdb.to_csv(outbase + 'greedyClusters.tsv', index=False, sep='\t')
    Ddb.to_csv(outbase + 'parsed_comparisonsTable_greedy.tsv', index=False, sep='\t')

def compare_Sprofiles_wrapper(IS1, IS_list, name1, names, scaffolds_to_compare, s2l, BIN_LENGTH, **kwargs):
    '''
    Compare IS1 to every IS in the IS_list
    '''
    table = defaultdict(list)
    cdbs = []
    for cIS, name2 in zip(IS_list, names):
        results = compare_Sprofiles(IS1, cIS, [name1, name2], scaffolds_to_compare, s2l, BIN_LENGTH, **kwargs)
        Cdb, ANI, cov = results
        table['name1'].append(name1)
        table['name2'].append(name2)
        table['ANI'].append(ANI)
        table['cov'].append(cov)
        for thing in ['g_ani', 'g_cov', 'g_mm']:
            table[thing].append(kwargs.get(thing, np.nan))
        cdbs.append(Cdb)

    Ndb = pd.concat(cdbs)
    Ddb = pd.DataFrame(table)
    return Ddb, Ndb


def compare_Sprofiles(IS1, cIS, names, scaffolds_to_compare, s2l, BIN_LENGTH, **kwargs):
    '''
    Compare all scaffolds of two Sprofiles
    '''
    Cdb, scaff2pair2mm2SNPs, scaff2pair2mm2cov = compare_scaffolds(names, [IS1, cIS], scaffolds_to_compare, s2l, **kwargs)
    ANI, cov = calc_cov_ani(Cdb, BIN_LENGTH, **kwargs)
    return Cdb, ANI, cov

def calc_cov_ani(Cdb, BIN_LENGTH, **kwargs):
    '''
    Return ANI and coverage assuming all scaffolds belong to the same genome
    '''
    g_ani = kwargs.get('g_ani', 0.99)
    g_cov = kwargs.get('g_cov', 0.5)
    g_mm = kwargs.get('g_mm', 100)

    db = Cdb[Cdb['mm'] <= g_mm].sort_values('mm').drop_duplicates(subset=['scaffold', 'name1', 'name2'], keep='last')
    tcb = sum(db['compared_bases_count'])

    if tcb == 0:
        popANI = np.nan
    else:
        popANI = sum(a * c  if a == a else 0 for a, c in
                            zip(db['popANI'], db['compared_bases_count'])) / tcb

    return popANI, (tcb / BIN_LENGTH)

def store_scaff2pair2mm2SNPs(obj, fileloc, convert=False):
    '''
    Store as an hdf5 object

    If convert, declare it as a numpy array from a set
    '''
    f = h5py.File(fileloc, "w")
    for scaff, pair2mm2SNPs in obj.items():
        for pair, mm2SNPs in pair2mm2SNPs.items():
            for mm, arr in mm2SNPs.items():
                if convert:
                    dset = f.create_dataset("{0}::{1}::{2}".format(scaff, pair, mm),
                            data=np.fromiter(arr, int, len(arr)), compression="gzip")
                else:
                    dset = f.create_dataset("{0}::{1}::{2}".format(scaff, pair, mm),
                            data=np.array(arr),compression="gzip")

def load_scaff2pair2mm2SNPs(location, scaffolds=[], pairs=[]):
    scaff2pair2mm2SNPs = {}
    f = h5py.File(location, 'r')

    for thing in list(f.keys()):
        scaff, pair, mm = thing.split('::')

        if scaffolds != []:
            if scaff not in scaffolds:
                continue

        if pairs != []:
            if pair not in pairs:
                continue

        dset = list(f[thing])
        mm = int(mm)

        if scaff not in scaff2pair2mm2SNPs:
            scaff2pair2mm2SNPs[scaff] = {}

        if pair not in scaff2pair2mm2SNPs[scaff]:
            scaff2pair2mm2SNPs[scaff][pair] = {}

        scaff2pair2mm2SNPs[scaff][pair][mm] = dset # convert from 2d array to series

    return scaff2pair2mm2SNPs

def parse_validate(args):
    '''
    Make sure there are some shared scaffolds between these
    '''
    inputs = list(args.input)
    assert len(inputs) > 1, "You need to have more than one input .IS file"

    outbase = args.output
    RCprof = inStrain.SNVprofile.SNVprofile(outbase)
    log_loc = RCprof.get_location('log') + 'log.log'
    inStrain.controller.setup_logger(log_loc)

    # Get the stuff to return
    names = []
    Sprofiles = []
    scaffolds = []
    scaffold2length = {}

    # Make a gloabl SNVprofile
    global name2SNVprofile
    name2SNVprofile = {}

    for inp in inputs:
        if not os.path.exists(inp):
            logging.error("IS {0} does not exist! Skipping".format(inp))
            continue

        logging.info("Loading {0}".format(inp))
        S = inStrain.SNVprofile.SNVprofile(inp)
        name = os.path.basename(S.get('bam_loc'))

        scaffolds += list(S._get_covt_keys())
        names.append(name)
        name2SNVprofile[name] = S.get('cumulative_snv_table')
        Sprofiles.append(S)

        s2l = S.get('scaffold2length')
        for scaff, l in s2l.items():
            if scaff in scaffold2length:
                assert l == scaffold2length[scaff]
            else:
                scaffold2length[scaff] = l

    # Figure out which scaffolds to compare
    scaffolds_to_compare = [x for x, c in pd.Series(scaffolds).value_counts().to_dict().items() if c >= 2]
    logging.info("{0} of {1} scaffolds are in at least 2 samples".format(
                    len(scaffolds_to_compare), len(set(scaffolds))))

    # Figure out scaffolds that are in the list
    if args.scaffolds != None:
        scaffolds = inStrain.controller.load_scaff_list(args.scaffolds)
        scaffolds_to_compare = list(set(scaffolds).intersection(set(scaffolds_to_compare)))
        logging.info("{0} of these scaffolds are in the provided list of {1}".format(
                        len(scaffolds_to_compare), len(set(scaffolds))))

    assert len(scaffolds_to_compare) > 0, "No scaffolds are shared amoung the IS"

    return RCprof, names, Sprofiles, scaffolds_to_compare, outbase, scaffold2length

# def parse_validate(args):
#     '''
#     Make sure there are some shared scaffolds between these
#
#     TO DO: make sure they're all the same lengths
#     '''
#     inputs = list(args.input)
#     assert len(inputs) > 1, "You need to have more than one input .IS file"
#
#     outbase = args.output
#     inStrain.controller.setup_logger(outbase + '.log')
#
#     # Get the stuff to return
#     names = []
#     Spprofiles = []
#     SNPtables = []
#
#     # Make a series of value counts
#     scaffolds = []
#
#     # Figure out scaffolds that are in the list
#     if args.scaffolds != None:
#         scaffold_list = inStrain.controller.load_scaff_list(args.scaffolds)
#     else:
#         scaffold_list = []
#
#     # Fill this stuff in
#     P2C = {'A':0, 'C':1, 'T':2, 'G':3, 'X':4}
#     for inp in inputs:
#         if not os.path.exists(inp):
#             logging.error("IS {0} does not exist! Skipping".format(inp))
#             continue
#
#         S = inStrain.SNVprofile.SNVprofile(inp)
#         scaffolds += S._get_covt_keys()
#         names.append(os.path.basename(S.get('bam_loc')))
#
#         covT = S.get('covT', scaffolds=scaffold_list)
#         scaffolds += list(covT.keys())
#         names.append(os.path.basename(S.get('bam_loc')))
#         covTs.append(covT)
#
#         db = S.get('cumulative_snv_table')
#         if len(db) > 0:
#             db['conBase'] = [x if x in P2C else 'X' for x in db['conBase']]
#             db['refBase'] = [x if x in P2C else 'X' for x in db['conBase']]
#
#             db['conBase'] = db['conBase'].map(P2C).astype('int8')
#             db['refBase'] = db['refBase'].map(P2C).astype('int8')
#         SNPtables.append(db)
#         #
#         # print(inp)
#         # x = len(covTs[-1]['N5_271_010G1_scaffold_100'])
#         # print(x)
#         # print(x/1148)
#
#         #SNPtables.append(S.get('cumulative_snv_table'))
#
#     # Figure out which scaffolds to compare
#     scaffolds_to_compare = [x for x, c in pd.Series(scaffolds).value_counts().to_dict().items() if c >= 2]
#     logging.info("{0} of {1} scaffolds are in at least 2 samples".format(
#                     len(scaffolds_to_compare), len(set(scaffolds))))
#
#     # # Figure out scaffolds that are in the list
#     # if args.scaffolds != None:
#     #     scaffolds = inStrain.controller.load_scaff_list(args.scaffolds)
#     #     scaffolds_to_compare = list(set(scaffolds).intersection(set(scaffolds_to_compare)))
#     #     logging.info("{0} of these scaffolds are in the provided list of {1}".format(
#     #                     len(scaffolds_to_compare), len(set(scaffolds))))
#
#     assert len(scaffolds_to_compare) > 0, "No scaffolds are shared amoung the IS"
#
#     return names, covTs, SNPtables, scaffolds_to_compare, outbase

def compare_scaffolds(names, Sprofiles, scaffolds_to_compare, s2l, **kwargs):
    '''
    Return a DataFrame with name1, name2, scaffold, ANI, coverage, ect.

    ScaffProfiles each contain [DataFrame of "scaffold, sample1, sample2, mm, coverage_overlap, ANI",
                                pair2mm2SNPlocs,
                                name of scaffold]

    Overview of how it works:
    * Iterate scaffolds that are in at least two of the samples
    * Set up the multiprocessing
    * Return the results
    '''
    # get arguments for the wrapper
    p = int(kwargs.get('processes', 6))

    ScaffProfiles = []
    if p > 1:
        ex = concurrent.futures.ProcessPoolExecutor(max_workers=p)

        total_cmds = len([x for x in iterate_commands(names, Sprofiles, s2l, scaffolds_to_compare, kwargs)])

        wait_for = [ex.submit(scaffold_profile_wrapper, cmd) for cmd in iterate_commands(names, Sprofiles, s2l, scaffolds_to_compare, kwargs)]

        for f in tqdm(futures.as_completed(wait_for), total=total_cmds, desc='Profiling scaffolds'):
            try:
                results = f.result()
                ScaffProfiles.append(results)
            except:
                logging.error("We had a failure! Not sure where!")

    else:
        for cmd in tqdm(iterate_commands(names, Sprofiles, s2l, scaffolds_to_compare, kwargs),
                        desc='Profiling scaffolds:',
                        total = len(scaffolds_to_compare)):
            ScaffProfiles.append(scaffold_profile_wrapper(cmd))

    # identity skipped scaffolds
    skipped = set([s[3][5:] for s in ScaffProfiles if s[3][:4] == 'skip'])

    # Retry the ones that failed but were not skipped
    failed_scaffs = set(scaffolds_to_compare) - set([s[3] for s in ScaffProfiles]) - skipped

    if len(failed_scaffs) > 0:
        logging.error("The following scaffolds failed- I'll try again {0}".format('\n'.join(failed_scaffs)))

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=p)
        total_cmds = len([x for x in iterate_commands(names, Sprofiles, s2l, failed_scaffs, kwargs)])
        wait_for = [ex.submit(scaffold_profile_wrapper, cmd) for cmd in iterate_commands(names, Sprofiles, s2l, failed_scaffs, kwargs)]
        for f in tqdm(futures.as_completed(wait_for), total=total_cmds, desc='Profiling failed scaffolds'):
            results = f.result()
            ScaffProfiles.append(results)

    # Do some processing to figure out which ones failed
    dbs = []
    mdbs = []
    scaff2pair2mm2cov = {}

    for s in ScaffProfiles:
        if len(s) != 4:
            logging.debug("Scaff profile is not len 4! Its {0}".format(s))
        elif s[3][:4] == 'skip':
            pass
        else:
            if isinstance(s[0], pd.DataFrame):
                dbs.append(s[0])
                mdbs.append(s[1])
                scaff2pair2mm2cov[s[3]] = s[2]
            else:
                logging.debug("THIS PROFILE IS NOT A DATAFRAME! {1} {0}".format(s, s[3]))

    if len(mdbs) > 0:
        Mdb = pd.concat(mdbs, sort=False)
    else:
        Mdb = pd.DataFrame()

    return [pd.concat(dbs, sort=False), Mdb, scaff2pair2mm2cov]

    # return [pd.concat([x[0] for x in ScaffProfiles]), # Table
    #         {s:i for i, s in zip([x[1] for x in ScaffProfiles], [x[3] for x in ScaffProfiles]) if s != 'skip'}, # scaff2pair2mm2SNPs
    #         {s:i for i, s in zip([x[2] for x in ScaffProfiles], [x[3] for x in ScaffProfiles]) if s != 'skip'}] # scaff2pair2mm2cov

def scaffold_profile_wrapper(cmd):
    '''
    Take a command and profile the scaffold
    '''
    logging.debug('running {0}'.format(cmd.scaffold))
    try:
        return compare_scaffold(cmd.scaffold, cmd.names, cmd.sProfiles, cmd.mLen, **cmd.arguments)
    except Exception as e:
        print(e)
        traceback.print_exc()
        logging.error("whole scaffold exception- {0}".format(str(cmd.scaffold)))
        return pd.DataFrame({'Failed':[True]})

def iterate_commands(names, sProfiles, s2l, scaffolds_to_compare, kwargs):
    '''
    Make and iterate profiling commands
    Doing it in this way makes it use way less RAM

    Here you're filtering such that only profiles that have the scaffold are compared
    '''
    for scaff in scaffolds_to_compare:
        # make this command
        cmd = profile_scaffold_command()
        cmd.scaffold = scaff

        names_cur = []
        sProfiles_cur = []

        for i, name in enumerate(names):
            names_cur.append(names[i])
            sProfiles_cur.append(sProfiles[i])

        cmd.names = names_cur
        cmd.sProfiles = sProfiles_cur
        cmd.arguments = kwargs
        cmd.mLen = s2l[scaff]

        yield cmd

def iterate_scaffold_chunks(scaffolds_to_compare, chunkSize=100):
    '''
    Break up scaffold list into chunks
    '''
    numberChunks = len(scaffolds_to_compare) // chunkSize + 1
    for i in range(numberChunks):
        yield (scaffolds_to_compare[i*chunkSize:(i+1)*chunkSize])

class profile_scaffold_command():
    '''
    This class just holds all of the mumbo-jumbo needed to profile a scaffold
    '''
    def __init__(self):
        pass

def _get_SNP_table(SNVprofile, scaffold, name):
    if 'name2SNVprofile' in globals():
        db = name2SNVprofile[name]
    else:
        db = SNVprofile.get('cumulative_snv_table')

    if len(db) > 0:
        db = db[db['scaffold'] == scaffold]
        if len(db) == 0:
            db = pd.DataFrame()
        else:
            db = db.sort_values('mm')
            #db = db[['position', 'mm', 'conBase', 'refBase', 'varBase', 'baseCoverage', 'A', 'C', 'T', 'G', 'allele_count']]
    else:
        db = pd.DataFrame()

    return db

def compare_scaffold(scaffold, names, sProfiles, mLen, **kwargs):
    '''
    This is the money method thats going to be multithreaded eventually

    Arguments:
        scaffold: name of scaffold (needs to match the SNPtables)
        names: names of the samples; must be in the same order as covTs and SNPtables
        sProfiles: a list of SNVprofiles that correspond to the names list
        mLen: length of the scaffold

    Returns:
        [DataFrame of "scaffold, sample1, sample2, mm, coverage_overlap, ANI",
        DataFrame of SNP locs,
        pair2mm2covOverlap,
        name of scaffold]
    '''
    # Load arguments
    min_cov = kwargs.get('min_cov', 5)
    min_freq = kwargs.get('min_freq', 5)
    debug = kwargs.get('debug', False)
    fdr = kwargs.get('fdr', 1e-6)
    store_coverage = kwargs.get('store_coverage_overlap', False)
    store_mm_locations = kwargs.get('store_mismatch_locations', False)
    include_self_comparisons = kwargs.get('include_self_comparisons', False)

    # Load covT and SNPtables
    covTs = []
    SNPtables = []
    cur_names = []
    for S, name in zip(sProfiles, names):
        covT = S.get('covT', scaffolds=[scaffold])
        if len(covT.keys()) == 0:
            continue

        covTs.append(covT[scaffold])
        db = _get_SNP_table(S, scaffold, name)

        SNPtables.append(db)
        cur_names.append(name)

    if len(cur_names) < 2:
        return [pd.DataFrame(), pd.DataFrame(), {}, 'skip_{0}'.format(scaffold)]

    # Iterate through pairs
    i = 0
    snpLocs = []
    pair2mm2covOverlap = {}
    table = defaultdict(list)
    for covT1, SNPtable1_ori, name1 in zip(covTs, SNPtables, cur_names):
        i += 1
        j = 0
        for covT2, SNPtable2_ori, name2 in zip(covTs, SNPtables, cur_names):
            j += 1

            if i > j:
                continue

            if (not include_self_comparisons) & (i == j):
                continue

            logging.debug("{2} {0} vs {1} ({3} {4})".format(name1, name2, scaffold,
                        i, j))

            if debug:
                pid = os.getpid()
                process  = psutil.Process(os.getpid())
                bytes_used = process.memory_info().rss
                total_available_bytes = psutil.virtual_memory()
                log_message = "\n{4} PID {0} end at {5} with {1} RAM. System has {2} of {3} available".format(
                        pid, bytes_used, total_available_bytes[1], total_available_bytes[0],
                        scaffold, time.time())
                logging.debug(log_message)

            mm2overlap, mm2coverage = _calc_mm2overlap(covT1, covT2, min_cov=min_cov, verbose=False, debug=debug)
            Mdb = _calc_SNP_count_alternate(SNPtable1_ori, SNPtable2_ori, mm2overlap, min_freq=min_freq, fdr=fdr, debug=debug)

            table = _update_overlap_table(table, scaffold, mm2overlap, mm2coverage, Mdb, name1, name2, mLen)

            if store_mm_locations:
                Mdb['name1'] = name1
                Mdb['name2'] = name2
                Mdb['scaffold'] = scaffold
                snpLocs.append(Mdb)

            if store_coverage:
                pair2mm2covOverlap['-vs-'.join(sorted([name1, name2]))] = mm2overlap

    logging.debug("Returning {0} {1} {2}".format(scaffold, i, j))

    Cdb = pd.DataFrame(table)
    if len(snpLocs) > 0:
        Mdb = pd.concat(snpLocs, sort=False)
    else:
        Mdb = pd.DataFrame()

    return [pd.DataFrame(table), Mdb, pair2mm2covOverlap, scaffold]

def _calc_mm2overlap(covT1, covT2, min_cov=5, verbose=False, debug=False):
    '''
    Calculate mm2overlap for a pair of covTs

    Coverage is calculated as cov = len(coveredInBoth) / len(coveredInEither)
    This means that its the percentage of bases that are covered by both

    Returns:
        mm2overlap -> dictionary to array of "True" where there's overlap and "False" where both are compared but there's not overlap
        mm2coverage -> dictionary of mm -> the alignment coverage
    '''
    mm2overlap = {}
    mm2coverage = {}

    # if debug != False:
    #     scaffold, name1, name2 = debug

    mms = sorted(list(set(covT1.keys()).union(set(covT2.keys()))))
    cov1 = pd.Series()
    cov2 = pd.Series()
    for mm in mms:
        if mm in covT1:
            cov1 = cov1.add(covT1[mm], fill_value=0)
        if mm in covT2:
            cov2 = cov2.add(covT2[mm], fill_value=0)

        # Figure out where each has min coverage
        T1 = set(cov1[(cov1 >= min_cov)].index)
        T2 = set(cov2[(cov2 >= min_cov)].index)

        # Figure out the total possible overlap
        coveredInEither = T1.union(T2)

        # Figure out where there's overlap in both
        coveredInBoth = T1.intersection(T2)

        # Calculate coverage
        if len(coveredInEither) > 0:
            cov = len(coveredInBoth) / len(coveredInEither)
        else:
            cov = 0

        # Save
        mm2overlap[mm] = coveredInBoth
        mm2coverage[mm] = cov

    return mm2overlap, mm2coverage
#
# def _keep(currentMM, rowMM, maxMM):
#     if ((currentMM > maxMM) & (rowMM == maxMM)):
#         return True
#     elif currentMM >= rowMM:
#         return True
#     else:
#         return False

# def _calc_SNP_count(SNPtable1, SNPtable2, mm2overlap, compare_consensus_bases=False, min_freq=0.05, fdr=1e-6, debug=False):
#     '''
#     For every mm, figure out the ANI
#
#     Returns:
#         [mm -> ANI, mm -> set of SNP locations]
#     '''
#     mm2ANI = {}
#     mm2SNPlocs = {}
#
#     if compare_consensus_bases != True:
#         null_loc = os.path.dirname(__file__) + '/helper_files/NullModel.txt'
#         model_to_use = inStrain.profileUtilities.generate_snp_model(null_loc, fdr=fdr)
#     else:
#         model_to_use = False
#
#     # if debug:
#     #     print("mms: {0}".format(list(mm2overlap.keys())))
#
#     for mm, cov_arr in mm2overlap.items():
#         snps = set()
#
#         # Bases that have coverage in both
#         covs = set(mm2overlap[mm])
#
#         # These represent relevant counts at these posisions
#         if len(SNPtable1) > 0:
#             s1_all = SNPtable1[[((m <= mm) & (p in covs))
#                             for p, c, r, m in zip(SNPtable1['position'].values, SNPtable1['conBase'].values,
#                             SNPtable1['refBase'].values, SNPtable1['mm'].values)]]
#         else:
#             s1_all = pd.DataFrame()
#
#         if len(SNPtable2) > 0:
#             s2_all = SNPtable2[[((m <= mm) & (p in covs))
#                             for p, c, r, m in zip(SNPtable2['position'].values, SNPtable2['conBase'].values,
#                             SNPtable2['refBase'].values, SNPtable2['mm'].values)]]
#         else:
#             s2_all = pd.DataFrame()
#
#
#         # These represent all cases where the consensus differs from the reference
#         if len(s1_all) > 0:
#             s1 = s1_all[s1_all['conBase'] != s1_all['refBase']]
#             p2c1 = {p:b for p, b in zip(s1_all['position'], s1_all['conBase'])}
#         else:
#             s1 = pd.DataFrame()
#
#         if len(s2_all) > 0:
#             s2 = s2_all[s2_all['conBase'] != s2_all['refBase']]
#             p2c2 = {p:b for p, b in zip(s2_all['position'], s2_all['conBase'])}
#         else:
#             s2 = pd.DataFrame()
#
#         # print("mm {0} - s1 {1} s2 {2}".format(mm, len(s1), len(s2)))
#
#         # If there are no places where the consensus sequences differ, continue with no SNPs
#         if ((len(s1) == 0) & (len(s2) == 0)):
#             pass
#
#         # If either of them has no SNPs at all, all consensus differences in the other are snps
#         elif len(s1_all) == 0:
#             if len(s2) > 0:
#                 snps = snps.union(set(s2['position'].values))
#
#
#         elif len(s2_all) == 0:
#             if len(s1) > 0:
#                 snps = snps.union(set(s1['position'].values))
#
#         # If you get here, it means you have SNPs called in both, and at least some differences from the diferences
#         else:
#
#             s_all_1 = set(s1_all['position'].values)
#             s_all_2 = set(s2_all['position'].values)
#
#             se1 = set(s1['position'].values)
#             se2 = set(s2['position'].values)
#
#             # For all cases where the consensus sequence differs in the first sample, check if it's a SNP in the second sample
#             s1_only = se1 - se2
#             for s in s1_only:
#                 if s in s_all_2:
#                     if _is_snp(s1_all, s2_all, s, p2c1, p2c2, min_freq=min_freq, model_to_use=model_to_use, compare_consensus_bases=compare_consensus_bases):
#                         if debug:
#                             print('adding {0}'.format(s))
#                         snps.add(s)
#                 else:
#                     snps.add(s)
#
#             # Same for the other
#             s2_only = se2 - se1
#             # if debug:
#             #     print("{0} is in s2_only: {1}".format(DEBUG, DEBUG in s2_only))
#             for s in s2_only:
#                 if s in s_all_1:
#                     if _is_snp(s1_all, s2_all, s, p2c1, p2c2, min_freq=min_freq, model_to_use=model_to_use, compare_consensus_bases=compare_consensus_bases):
#                         snps.add(s)
#                 else:
#                     snps.add(s)
#
#             # maybe SNPs are those that the consensus disagrees with the reference in both cases
#             maybe_snps = se1.intersection(se2)
#             # if debug:
#             #     print("{0} is in maybe_snps: {1}".format(DEBUG, DEBUG in maybe_snps))
#             #     print(maybe_snps)
#             for m in maybe_snps:
#                 # sdbug=False
#                 # if (m == DEBUG) & (debug):
#                 #     sdbug=True
#                 if _is_snp(s1_all, s2_all, m, p2c1, p2c2, min_freq=min_freq, model_to_use=model_to_use, compare_consensus_bases=compare_consensus_bases):
#                     snps.add(m)
#
#         if debug:
#             print("here are the snps: {0}".format(snps))
#         cov = len(covs)
#         mm2SNPlocs[mm] = np.array(list(snps), dtype='int')
#         if cov == 0:
#             mm2ANI[mm] = np.nan
#         else:
#             mm2ANI[mm] = (cov - len(snps)) / cov
#
#     return mm2ANI, mm2SNPlocs

def _gen_blank_Mdb():
    COLUMNS = ['position', 'conBase_1', 'refBase_1', 'varBase_1', 'baseCoverage_1',
       'A_1', 'C_1', 'T_1', 'G_1', 'conBase_2', 'refBase_2', 'varBase_2',
       'baseCoverage_2', 'A_2', 'C_2', 'T_2', 'G_2', 'consensus_SNP', 'population_SNP', 'mm']
    return pd.DataFrame({c:[] for c in COLUMNS})

def _gen_blank_SNPdb():
    COLUMNS = ['position', 'conBase', 'refBase', 'varBase', 'baseCoverage',
       'A', 'C', 'T', 'G']
    return pd.DataFrame({c:[] for c in COLUMNS})

def _calc_SNP_count_alternate(SNPtable1, SNPtable2, mm2overlap, min_freq=.05, fdr=1e-6, debug=False):

    mm2ANI = {}
    mm2popANI = {}
    dbs = []

    # Get the null model for SNP calling
    null_loc = os.path.dirname(__file__) + '/helper_files/NullModel.txt'
    model_to_use = inStrain.profileUtilities.generate_snp_model(null_loc, fdr=fdr)

    # Iterate mm levels
    for mm, cov_arr in mm2overlap.items():

        # Subset to bases that have coverage in both
        covs = set(cov_arr)

        # These represent relevant counts at these posisions
        if len(SNPtable1) > 0:
            s1_all = SNPtable1[[(p in covs) for p in SNPtable1['position'].values]].drop_duplicates(
                        subset=['position'], keep='last').drop(columns='mm')
        else:
            s1_all = _gen_blank_SNPdb()

        if len(SNPtable2) > 0:
            s2_all = SNPtable2[[(p in covs) for p in SNPtable2['position'].values]].drop_duplicates(
                        subset=['position'], keep='last').drop(columns='mm')
        else:
            s2_all = _gen_blank_SNPdb()

        # Merge
        if (len(s1_all) == 0) & (len(s2_all) == 0):
            Mdb = _gen_blank_Mdb()

        else:
            Mdb = pd.merge(s1_all, s2_all, on='position', suffixes=('_1', '_2'), how='outer', copy=False)
            Mdb['consensus_SNP'] = Mdb.apply(call_con_snps, axis=1)
            Mdb['population_SNP'] = Mdb.apply(call_pop_snps, axis=1, args=(model_to_use, min_freq))
            Mdb['mm'] = mm

        dbs.append(Mdb)

    Mdb = pd.concat(dbs, sort=False)
    return Mdb

def call_con_snps(row):
    '''
    Call a SNP if the consensus sequnces aren't the same
    '''
    return row['conBase_1'] != row['conBase_2']

def is_present(counts, total, model, min_freq):
    '''
    Return true if the base counts represented by "counts" are detected above background
    '''
    return (counts >= model[total]) and ((float(counts) / total) >= min_freq)

def call_pop_snps(row, model, min_freq):
    '''
    To be applied to a DataFrame

    Call a SNP if you can't find the consenus of 1 in 2 AND
    you can't find the consensus of 2 in 1 AND
    1 and 2 don't share a minor allele
    '''
    # Are the consensus bases the same?
    if row['conBase_1'] == row['conBase_2']:
        return False

    # Is it a SNP in only one? If so, see if the reference is still there
    if (row['conBase_1'] != row['conBase_1']) | (row['conBase_2'] != row['conBase_2']):

        # In this case, is consensus allele still detected?
        if (row['conBase_1'] != row['conBase_1']):
            count = row['{0}_2'.format(row['refBase_2'])]
            total = row['baseCoverage_2']
            if is_present(count, total, model, min_freq):
                return False

        elif (row['conBase_2'] != row['conBase_2']):
            count = row['{0}_1'.format(row['refBase_1'])]
            total = row['baseCoverage_1']
            if is_present(count, total, model, min_freq):
                return False

        return True

    ### OK, so it's a SNP in both ###

    # Look for conBase_1 in sample 2
    try:
        count = row["{0}_2".format(row['conBase_1'])]
        total = row['baseCoverage_2']
        if is_present(count, total, model, min_freq):
            return False
    except:
        print(row)

    # Look for conBase_2 in sample 1
    count = row["{0}_1".format(row['conBase_2'])]
    total = row['baseCoverage_1']
    if is_present(count, total, model, min_freq):
        return False

    # Look for minor in both samples
    if 'allele_count_1' in row:
        if (row['allele_count_1'] > 1) & (row['allele_count_2'] > 1):
            if row['varBase_1'] == row['varBase_2']:
                return False

    elif 'morphia_1' in row:
        if (row['morphia_1'] > 1) & (row['morphia_2'] > 1):
            if row['varBase_1'] == row['varBase_2']:
                return False

    return True

C2P = {0:'A', 1:'C', 2:'T', 3:'G'}
def _is_snp(db1, db2, position, p2c1, p2c2, min_freq, model_to_use, debug=False, compare_consensus_bases=False):
    '''
    Determine if the consensus base of db1 is a SNP in db2 and vice versa
    '''
    # Try and out quick
    if p2c1[position] == p2c2[position]:
        return False
    if compare_consensus_bases:
        return True

    # otime = time.time()
    # print("checkpoint {0} {1}".format(0, time.time() - otime))
    # These are sorted by mm above
    dd1 = db1[db1['position'] == position]#.sort_values('mm', ascending=False)#.drop_duplicates(subset='position', keep='last')
    dd2 = db2[db2['position'] == position]#.sort_values('mm', ascending=False)#.drop_duplicates(subset='position', keep='last')

    #print("checkpoint {0} {1}".format(1, time.time() - otime))

    assert len(dd1) > 0, [position, 'one']
    assert len(dd2) > 0, [position, 'two']

    #print("checkpoint {0} {1}".format(2, time.time() - otime))

    con1 = p2c1[position]
    con2 = p2c2[position]

    #print("{0} in {1} is {2}".format(position, p2c1, con1))

    #print("checkpoint {0} {1}".format(3, time.time() - otime))

    # Check if the consensus of db1 is a SNP in db2
    #counts2 = dd2.iloc[-1][C2P[con1]]
    counts2 = dd2.iloc[-1][con1]
    total = dd2.iloc[-1]['baseCoverage']
    if (counts2 >= model_to_use[total]) and ((float(counts2) / total) >= min_freq):
        pass
    else:
        return True

    # Check the opposite
    #counts1 = dd1.iloc[-1][C2P[con2]]
    counts1 = dd1.iloc[-1][con2]
    total = dd1.iloc[-1]['baseCoverage']
    if (counts1 >= model_to_use[total]) and ((float(counts1) / total) >= min_freq):
        pass
    else:
        return True

    # Looks like you're not a SNP, then!
    if debug:  print('exit2')

    #print("checkpoint {0} {1}".format(5, time.time() - otime))

    return False

def _update_overlap_table(table, scaffold, mm2overlap, mm2coverage, Mdb, name1, name2, mLen):
    '''
    covarage_overlap = the percentage of bases that are either covered or not covered in both
        - So if both scaffolds have 0 coverage, this will be 1
    percent_genome_compared = the percentage of bases in the scaffolds that are covered by both
        - So if both scaffolds have 0 coverave, this will be 0
    compared_bases_count = the number of considered bases
    '''
    for mm, overlap in mm2overlap.items():
        bases = len(overlap)
        mdb = Mdb[Mdb['mm'] == mm]

        table['mm'].append(mm)
        table['scaffold'].append(scaffold)
        table['name1'].append(name1)
        table['name2'].append(name2)
        table['coverage_overlap'].append(mm2coverage[mm])
        table['compared_bases_count'].append(bases)
        table['percent_genome_compared'].append(bases/mLen)
        table['length'].append(mLen)

        snps = len(mdb[mdb['consensus_SNP'] == True])
        popsnps = len(mdb[mdb['population_SNP'] == True])

        table['consensus_SNPs'].append(snps)
        table['population_SNPs'].append(popsnps)

        if bases == 0:
            table['popANI'].append(np.nan)
            table['conANI'].append(np.nan)
        else:
            table['conANI'].append((bases - snps) / bases)
            table['popANI'].append((bases - popsnps) / bases)

    return table

# if __name__ == '__main__':
#
#     parser = argparse.ArgumentParser(description= """
#         A script that compares multiple runs of inStrain\n
#         """, formatter_class=argparse.RawTextHelpFormatter)
#
#     # Required positional arguments
#     parser.add_argument('-i', '--input', help="A list of inStrain objects, all mapped to the same .fasta file",
#                         nargs='*', required=True)
#     parser.add_argument("-o", "--output", action="store", default='instrainComparer', \
#                         help='Output prefix')
#     parser.add_argument("-s", "--scaffolds", action="store", \
#                         help='Location to a list of scaffolds to compare. You can also make this a .fasta file and it will load the scaffold names')
#     parser.add_argument("-c", "--min_cov", action="store", default=5, type=int, \
#                         help='Minimum SNV coverage (for coverage calculations)')
#     parser.add_argument("-f", "--min_freq", action="store", default=0.05, type=float, \
#         help='Minimum SNP frequency to confirm a SNV (both this AND the 0.001 percent FDR snp count cutoff must be true). If compare_consensus_bases is set, this doesnt matter')
#     parser.add_argument("-fdr", "--fdr", action="store", default=1e-6, type=float, \
#         help='SNP false discovery rate- based on simulation data with a 0.1 percent error rate (Q30).  If compare_consensus_bases is set, this doesnt matter')
#     parser.add_argument("-p", "--processes", action="store", default=6, type=int, \
#                         help='Threads to use for multiprocessing')
#
#     parser.add_argument('--store_coverage_overlap', action='store_true', default=False,\
#         help="Also store coverage overlap on an mm level")
#     parser.add_argument('--skip_mismatch_locations', action='store_true', default=False,\
#         help="Dont store the locations of SNPs")
#     parser.add_argument('--compare_consensus_bases', action='store_true', default=False,\
#         help="Only compare consensus bases; dont look for lower frequency SNPs when calculating ANI")
#     parser.add_argument('--include_self_comparisons', action='store_true', default=False,\
#         help="Also compare IS profiles against themself")
#
#
#     args = parser.parse_args()
#     main(args)
