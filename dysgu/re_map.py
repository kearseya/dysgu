from skbio.alignment import StripedSmithWaterman
from dysgu.map_set_utils import is_overlapping
from dysgu.coverage import merge_intervals

import edlib
import click
import logging


def get_clipped_seq(cont, position, cont_ref_start, cont_ref_end):
    if cont and len(cont) > 5:
        start_i = 1
        while cont[start_i].islower():
            start_i += 1
            if start_i == len(cont):
                break
        end_i = len(cont) - 1
        while cont[end_i].islower():
            end_i -= 1
            if end_i < 0:
                break
        if abs(cont_ref_start - position) < abs(cont_ref_end - position):
            if start_i > 8:
                left_clip = cont[:start_i]
                return left_clip, 0, len(cont) - end_i
        else:
            if len(cont) - end_i > 8:
                right_clip = cont[end_i + 1:]
                return right_clip, 1, start_i


def filter_bad_alignment(align, event, idx, begin, end, break_position):
    pos = event["pos" + idx]
    score = align.optimal_alignment_score

    span = align.target_end_optimal + 1 - align.target_begin
    seq1 = align.aligned_query_sequence
    seq2 = align.aligned_target_sequence

    if not seq1 or not seq2:
        return -1
    if align.target_begin > 8 and len(align.target_sequence) - align.target_end_optimal > 8:
        return -2

    distance_to_break = min(abs(begin - break_position), abs(end - break_position))
    large_gap_penalty = 24
    gapped_score = score

    if distance_to_break > 200:
        gapped_score = score - large_gap_penalty

    expect_thresh = 0.7

    if gapped_score > 12:
        if is_overlapping(begin - 1, end + 1, pos, pos + 1):

            # if total unmapped is too large reject
            if len(align.target_sequence) - span > span * 0.8 and score < 30:
                return -3

            return 1
        elif gapped_score > 20:
            expected = span * 2  # 2 x match score
            # if gaps at both ends of alignment increase stringency
            if align.target_begin >= 2 and align.target_end_optimal < len(align.target_sequence) - 2:
                expected = span * 4
                # if total unmapped is too large reject
                if len(align.target_sequence) - span > span * 0.8 and gapped_score < 30:
                    return -4

            if span > 12 and float(score) / expected > expect_thresh:
                return expected - score
            else:
                return -5
    return -6


def merge_align_regions(locations):
    # Merge any similar alignment regions found by edlib, used to get the bounds of the alignment region
    if len(locations) <= 1:
        return locations
    merge_dist = 10
    new_l = []
    for s, e in locations:
        if len(new_l) == 0:
            new_l.append([s, e])
        last = new_l[-1]

        if abs(s - last[0]) < merge_dist and abs(e - last[1]) < merge_dist:
            new_l[-1][1] = e
        else:
            return None
    return new_l


def switch_sides(e):
    # switch sides
    chrA, posA, cipos95A, contig2  = e["chrA"], e["posA"], e["cipos95A"], e["contig2"]
    contig2_ref_start, contig2_ref_end, contig2_left_weight, contig2_right_weight = e["contig_ref_start"], e["contig_ref_end"], e["contig_left_weight"], e["contig_right_weight"]
    # e["chrA"] = e["chrB"] assumed to be the same
    e["posA"] = e["posB"]
    e["cipos95A"] = e["cipos95B"]
    e["chrB"] = chrA
    e["posB"] = posA
    e["cipos95B"] = cipos95A

    e["contig2"] = e["contig"]
    e["contig"] = contig2

    e["contig_ref_start"] = e["contig2_ref_start"]
    e["contig2_ref_start"] = contig2_ref_start

    e["contig_ref_end"] = e["contig2_ref_end"]
    e["contig2_ref_end"] = contig2_ref_end

    e["contig_left_weight"] = e["contig2_left_weight"]
    e["contig2_left_weight"] = contig2_left_weight

    e["contig_right_weight"] = e["contig2_right_weight"]
    e["contig2_right_weight"] = contig2_right_weight
    return e


def remap_soft_clips(events, ref_genome, min_sv_len, input_bam, keep_unmapped=True, keep_small=False, min_support=3):

    new_events = []
    ref_locs = []

    for count, e in enumerate(events):

        e["remapped"] = 0
        e["remap_score"] = 0
        e["remap_ed"] = 0
        e["scw"] = 0
        if 'svlen_precise' not in e:
            e['svlen_precise'] = 1

        if e["chrA"] != e["chrB"]:
            new_events.append(e)
            continue

        try_remap = False
        if (e["contig"] or e["contig2"]) and (e["svlen"] < 1000):
            if not e['svlen_precise']:
                try_remap = True

        if not try_remap:
            e["modified"] = 0
            new_events.append(e)
            continue

        else:
            # check if contig seq has lon enough soft-clip
            remap = False
            for cont, idx in (("contig", "A"), ("contig2", "B")):
                if cont in e and e[cont]:

                    break_position = e["pos" + idx]
                    clip_res = get_clipped_seq(e[cont], break_position, e[cont + "_ref_start"], e[cont + "_ref_end"])
                    if clip_res:
                        remap = True
                        e[cont + idx + "clip_res"] = clip_res

            if remap:
                if e["posA"] <= e["posB"]:
                    ref_locs.append((e["chrA"], e["posA"], e["posB"], count))
                else:
                    ref_locs.append((e["chrA"], e["posB"], e["posA"], count))

    for chrom, gstart, gend, grp_idxs in merge_intervals(ref_locs, pad=1500, add_indexes=True):
        if gstart < 0:
            gstart = 0
        try:
            ref_seq_big = ref_genome.fetch(chrom, gstart, gend).upper()
        except (ValueError, KeyError, IndexError) as errors:
            # Might be different reference genome version, compared to bam genome
            logging.warning("Error fetching reference chromosome: {}".format(chrom), errors)
            continue

        # note this doesnt parellelize well with multiprocessing.pool, suspect serializing is too slow
        # could try and parallel before the fetch command above using publish/subscribe model
        for index in grp_idxs:

            e = events[index]
            added = 0
            high_quality_clip = False
            skip_event = False
            max_clip_length = 0

            e["scw"] = max(e["contig_left_weight"], e["contig_right_weight"])
            for cont, idx in (("contig", "A"), ("contig2", "B")):
                if cont in e and e[cont]:

                    break_position = e["pos" + idx]
                    clip_res = e[cont + idx + "clip_res"]
                    if not clip_res:
                        continue

                    clip_seq, clip_side, length_other_clip = clip_res
                    if length_other_clip > 3 and e['ref_bases'] < 50:
                        continue

                    if len(clip_seq) > max_clip_length:
                        max_clip_length = len(clip_seq)

                    if clip_side == 0:
                        w = e[cont + "_left_weight"]
                        if not w > 10:  # todo set as a parameter option
                            continue

                    else:
                        w = e[cont + "_right_weight"]
                        if not w > 10:
                            continue

                    avg_w = w / len(clip_seq)
                    if avg_w > 1 or len(clip_seq) > 35 or w > 400:
                        high_quality_clip = True

                    ref_start = break_position - 500  # todo set as parameter
                    ref_end = break_position + 500

                    start_idx = ref_start - gstart
                    start_idx = 0 if start_idx < 0 else start_idx
                    end_idx = ref_end - gstart

                    ref_seq_clipped = ref_seq_big[start_idx:end_idx]
                    ref_seq_start = gstart + start_idx

                    if not ref_seq_clipped or ref_seq_clipped[0] in "nN" or ref_seq_clipped[-1] in "nN":
                        skip_event = True
                        break

                    # Large alignment region
                    el = edlib.align(clip_seq.upper(), ref_seq_clipped, mode="HW", task="locations")
                    locs = merge_align_regions(el['locations'])
                    if not locs:
                        continue

                    l_start, l_end = locs[0]
                    ref_start2 = ref_seq_start + l_start
                    ref_seq2 = ref_seq_clipped[l_start:l_end+1]

                    aln = StripedSmithWaterman(ref_seq2, match_score=2, mismatch_score=-8, gap_open_penalty=6, gap_extend_penalty=1)

                    a = aln(clip_seq)

                    score = a.optimal_alignment_score
                    aln_q_end = a.query_end
                    aln_q_begin = a.query_begin
                    aln_t_begin = a.target_begin
                    target_end_optimal = a.target_end_optimal
                    aln_t_end_unmapped = len(clip_seq) - target_end_optimal
                    q_begin = ref_start2 + aln_q_begin
                    q_end = ref_start2 + aln_q_end
                    edit_dist = filter_bad_alignment(a, e, idx, q_begin, q_end, break_position)

                    if not edit_dist < 0:

                        pos = e["pos" + idx]
                        if clip_side == 0:
                            if q_end + 1 >= pos:
                                # insertion might be tandem (continuous) or be novel sequence (gap in alignment)
                                kind = "INS"
                                break_point = pos
                                break_point2 = pos

                                overlap = 0
                                if q_end > pos:
                                    overlap = q_end - pos  # tandem

                                svlen = overlap + aln_t_end_unmapped

                            else:
                                ref_gap = pos - q_end
                                target_gap = len(clip_seq) - target_end_optimal

                                if target_gap > ref_gap:
                                    kind = "INS"
                                    break_point = pos
                                    break_point2 = pos
                                    svlen = target_gap

                                else:
                                    kind = "DEL"
                                    break_point = pos
                                    break_point2 = q_end
                                    svlen = ref_gap

                            # discard alignments with large unmapped overhang
                            if aln_t_begin > svlen:
                                continue
                        else:
                            if q_begin - 1 <= pos:
                                kind = "INS"
                                break_point = pos
                                break_point2 = pos

                                dangling_bases = len(clip_seq) - target_end_optimal
                                if dangling_bases > 20:
                                    svlen = len(clip_seq)
                                else:
                                    svlen = pos - q_begin

                                svlen += aln_t_begin

                            else:
                                ref_gap = q_begin - pos
                                target_gap = aln_t_begin
                                if target_gap > ref_gap:
                                    kind = "INS"
                                    break_point = pos
                                    break_point2 = pos
                                    svlen = target_gap
                                else:
                                    kind = "DEL"
                                    break_point = pos
                                    break_point2 = q_begin
                                    svlen = abs(break_point2 - break_point)

                            if len(clip_seq) - target_end_optimal > svlen:
                                continue

                        if kind == "DEL":
                            span = a.query_end - a.query_begin + 1

                            if span < len(clip_seq) * 0.4 and span < 50:
                                continue

                        if abs(svlen - e['svlen']) > 20:
                            e["remap_ed"] = edit_dist
                            e["remapped"] = 1
                            e["remap_score"] = score
                            e['svtype'] = kind
                            e['svlen'] = svlen
                            e['pos' + idx] = break_point
                            if idx == "A":
                                other = "B"
                            else:
                                other = "A"
                            e['pos' + other] = break_point2
                            e['cipos95A'] = 0
                            e['cipos95B'] = 0

                            # switch if nessasary
                            if e['posA'] > e['posB']:
                                e = switch_sides(e)

                            new_events.append(e)
                            added = 1
                            break  # dont analyse contig2

                if added:
                    break

            if not added and not skip_event and high_quality_clip and keep_unmapped and max_clip_length >= 18:
                # basic filter
                if e["su"] > min_support + 4:
                    new_events.append(e)

    return new_events


def drop_svs_near_reference_gaps(events, paired_end, ref_genome):

    ref_locs = []
    for count, e in enumerate(events):

        if e["spanning"] > 0:
            continue

        if e["chrA"] == e["chrB"]:
            if paired_end:
                if e["svtype"] == "INS" and e["svlen"] < 250:
                    continue
                elif e["svtype"] != "INS" and e["svlen"] < 1000:
                    continue
            elif e["svlen"] < 1000:
                continue

        ref_locs.append((e["chrA"], e["posA"] - 250, e["posA"] + 250, count))
        ref_locs.append((e["chrB"], e["posB"] - 250, e["posB"] + 250, count))

    if len(ref_locs) == 0:
        return events

    bad_i = set([])
    for chrom, gstart, gend, grp_idxs in merge_intervals(ref_locs, pad=0, add_indexes=True):

        s_gi = set(grp_idxs)
        s_gi -= bad_i
        if len(s_gi) == 0:
            continue

        if gstart < 0:
            gstart = 0
        try:
            ref_seq_big = ref_genome.fetch(chrom, gstart, gend).upper()
        except (ValueError, KeyError, IndexError) as errors:
            # Might be different reference genome version, compared to bam genome
            logging.warning("Error fetching reference chromosome: {}".format(chrom), errors)
            continue

        if ref_seq_big[0] == "N" or ref_seq_big[-1] == "N" or ref_seq_big[int(len(ref_seq_big) / 2)] == "N":
            bad_i |= s_gi

    new_events = [events[i] for i in range(len(events)) if i not in bad_i]
    logging.info("N near gaps gropped {}".format(len(bad_i)))

    return new_events