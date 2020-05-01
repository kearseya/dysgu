from __future__ import absolute_import
import click
from click.testing import CliRunner
import os
import time
from multiprocessing import cpu_count
import pkg_resources
import warnings
from . import cluster, view, sv2bam, sv2fq

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.simplefilter(action='ignore', category=FutureWarning)

cpu_range = click.IntRange(min=1, max=cpu_count())

defaults = {
            "clip_length": 30,
            "output": "-",
            "svs_out": "-",
            "max_cov": 500,
            "buffer_size": 0,
            "min_support": 4,
            "template_size": "210,175,125",
            "model": None,
            "max_tlen": 800,
            "z_depth": 2,
            "z_breadth": 3,
            "regions_only": "False",
            "soft_search": "True"
            }

version = pkg_resources.require("dysgu")[0].version


def apply_ctx(ctx, kwargs):
    click.echo("[dysgu] Version: {}".format(version), err=True)
    ctx.ensure_object(dict)
    if len(ctx.obj) == 0:  # When invoked from cmd line, else run was invoked from test function
        for k, v in list(defaults.items()) + list(kwargs.items()):
            ctx.obj[k] = v

    return ctx


@click.group(chain=False, invoke_without_command=False)
@click.version_option()
def cli():
    """Dysgu-SV is a set of tools for mapping and calling structural variants from sam/bam/cram files"""
    pass


@cli.command("fetch")
@click.argument('bam', required=True, type=click.Path(exists=True))
@click.option("-f", "--out-format", help="Output format. 'bam' output maintains sort order, "
                                         "'fq' output is collated by name",
              default="bam", type=click.Choice(["bam", "fq", "fasta"]),
              show_default=True)
@click.option("-o", "--output", help="Output file for all input alignments, use '-' or 'stdout' for stdout",
              default="None",
              type=str, show_default=True)
@click.option("-r", "--reads", help="Output reads, discordant, supplementary and soft-clipped reads to file. "
                                    "If --out-format is fq/fasta and --reads2 is not provided, "
                                    "output an interleaved fq/fasta", default="stdout", type=str, show_default=True)
@click.option("-r2", "--reads2", help="Output read2 for fq/fasta output only", default="None", type=str,
              show_default=True)
@click.option('--clip-length', help="Minimum soft-clip length, > threshold are kept", default=defaults["clip_length"],
              type=int, show_default=True)
@click.option('--index', help="Call 'samtools index' of bam if '--reads' argument is provided",
              default="False", type=click.Choice(["True", "False"]), show_default=True)
@click.option("--paired", help="Paired-end reads or single", default="True",
              type=click.Choice(["True", "False"]), show_default=True)
@click.option("-p", "--procs", help="Compression threads to use for writing bam", type=cpu_range, default=1,
              show_default=True)
@click.option('--search', help=".bed file, limit search to regions", default=None, type=click.Path(exists=True))
@click.option('--exclude', help=".bed file, do not search/call SVs within regions. Overrides include/search",
              default=None, type=click.Path(exists=True))
@click.pass_context
def get_reads(ctx, **kwargs):
    """Filters input .bam/.cram for read-pairs that are discordant or have a soft-clip of length > '--clip-length',
    writes bam/fq/fasta"""
    if kwargs["output"] in "-,stdout" and (kwargs["reads"] in "-,stdout" or kwargs["reads2"] in "-,stdout"):
        raise ValueError("--output and --reads/--reads2 both set to stdout")

    if kwargs["out_format"] in "fq,fasta":
        if kwargs["reads"] in "-,stdout" and kwargs["reads2"] != "None":
            raise ValueError("-r is set to stdout but -r2 is set to TEXT")
        if kwargs["reads2"] in "-,stdout":
            raise ValueError("-r2 can not be stdout")

    if kwargs["out_format"] == "bam" and kwargs["reads2"] != "None":
        raise ValueError("--out-format is bam, cannot except -r2")

    if kwargs["out_format"] == "bam" and kwargs["index"] == "True" and kwargs["reads"] in "-,stdout":
        raise ValueError("Cannot index if --reads is stdout")

    ctx = apply_ctx(ctx, kwargs)

    if kwargs["out_format"] == "bam":
        return sv2bam.process(ctx.obj)
    else:
        return sv2fq.process(ctx.obj)


@cli.command("call")
@click.argument('sv-aligns', required=True, type=click.Path(exists=False))
@click.option("-o", "--svs-out", help="Output file, [default: stdout]", required=False, type=click.Path())
@click.option("-f", "--out-format", help="Output format", default="vcf", type=click.Choice(["csv", "vcf"]),
              show_default=True)
@click.option('--clip-length', help="Minimum soft-clip length, > threshold are kept.", default=defaults["clip_length"],
              type=int, show_default=True)
@click.option('--max-cov', help="Regions with > max-cov that do no overlap 'include' are discarded",
              default=defaults["max_cov"], type=float, show_default=True)
@click.option('--max-tlen', help="Maximum template length to consider when calculating paired-end template size",
              default=defaults["max_tlen"], type=int, show_default=True)
@click.option('--min-support', help="Minimum number of reads per SV",
              default=defaults["min_support"], type=int, show_default=True)
@click.option('--mq', help="Minimum map quality < threshold are discarded", default=1,
              type=int, show_default=True)
@click.option('--z-depth', help="Minimum minimizer depth across alignments",
              default=defaults["z_depth"], type=int, show_default=True)
@click.option('--z-breadth', help="Minimum number of minimizers shared between a pair of alignments",
              default=defaults["z_breadth"], type=int, show_default=True)
@click.option("-I", "--template-size", help="Manually set insert size, insert stdev, read_length as 'INT,INT,INT'",
              default="", type=str, show_default=False)
@click.option('--regions-only', help="If --include is provided, call only events within target regions",
              default="False", type=click.Choice(["True", "False"]),
              show_default=True)
@click.option("-p", "--procs", help="Processors to use", type=cpu_range, default=1, show_default=True)
@click.option('--include', help=".bed file, limit calls to regions", default=None, type=click.Path(exists=True))
@click.option('--dest', help="Folder to use/create for saving results. Defaults to current directory",
              default=None, type=click.Path())
@click.option("--buffer-size", help="Number of alignments to buffer", default=defaults["buffer_size"],
              type=int, show_default=True)
@click.option("--merge-within", help="Try and merge similar events, recommended for most situations",
              default="True", type=click.Choice(["True", "False"]), show_default=True)
@click.option("--merge-dist", help="Distance threshold for merging, default is (insert-median + 5*insert_std) for paired"
                                   "reads, or 1000bp for single-end reads",
              default=None, type=int, show_default=False)
@click.option("--paired", help="Paired-end reads or single", default="True",
              type=click.Choice(["True", "False"]), show_default=True)
@click.option("--contigs", help="Generate consensus contigs for each side of break", default="True",
              type=click.Choice(["True", "False"]), show_default=True)
@click.pass_context
def call_events(ctx, **kwargs):
    """Call structural vaiants"""
    # Create dest in not done so already
    ctx = apply_ctx(ctx, kwargs)
    cluster.cluster_reads(ctx.obj)


@cli.command("view")
@click.argument('input_files', required=True, type=click.Path(), nargs=-1)
@click.option("-o", "svs_out", help="Output file, [default: stdout]", required=False, type=click.Path())
@click.option("-f", "--out-format", help="Output format", default="vcf", type=click.Choice(["csv", "vcf"]),
              show_default=True)
@click.option("--merge-across", help="Merge records across input samples", default="True",
              type=click.Choice(["True", "False"]), show_default=True)
@click.option("--merge-within", help="Perform additional merge within input samples, prior to --merge-across",
              default="False", type=click.Choice(["True", "False"]), show_default=True)
@click.option("--merge-dist", help="Distance threshold for merging",
              default=25, type=int, show_default=True)
@click.option("--separate", help="Keep merged tables separate, adds --post-fix to file names, csv format only",
              default="False", type=click.Choice(["True", "False"]), show_default=True)
@click.option("--post-fix", help="Adds --post-fix to file names, only if --separate is True",
              default="dysgu", type=str, show_default=True)
@click.option("--no-chr", help="Remove 'chr' from chromosome names in vcf output", default="False",
              type=click.Choice(["True", "False"]), show_default=True)
@click.option("--no-contigs", help="Remove contig sequences from vcf output", default="False",
              type=click.Choice(["True", "False"]), show_default=True)
@click.option("--add-kind", help="Add region-overlap 'kind' to vcf output", default="False",
              type=click.Choice(["True", "False"]), show_default=True)
@click.pass_context
def view_data(ctx, **kwargs):
    """Convert .csv table(s) to .vcf. Merges multiple .csv files into wide .vcf format."""
    # Add arguments to context insert_median, insert_stdev, read_length, out_name
    ctx = apply_ctx(ctx, kwargs)
    return view.view_file(ctx.obj)


@cli.command("test", context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.pass_context
def test_command(ctx, **kwargs):
    """Run dysgu tests"""

    tests_path = os.path.dirname(__file__) + "/tests"
    runner = CliRunner()

    t = [tests_path + '/small.bam', '--post-fix', 'dysgu_test']
    click.echo(t)
    result = runner.invoke(sv2fq, t)

    t = [tests_path + '/small.bam', '--post-fix', 'dysgu_test']
    click.echo(t)
    result = runner.invoke(sv2bam, t)

    t = [tests_path + '/small.bam', '--template-size', '350,100', '-o', './small.dysgu_test.csv']
    click.echo(t)
    result = runner.invoke(call_events, t)

    click.echo("Done", err=True)

