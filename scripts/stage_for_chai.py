# Copyright (c) 2024 Chai Discovery, Inc.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for details.
"""
Given a output directory from a ColabFold run, traverses the directory structure and stage
the same MSA and templates to run through Chai1.

Some minimal example:

Given the following directory structure:
    colab_out_dir/
        - 4nnp_env/
        - 4nnp_pairgreedy/
        ...
        - sequences.csv (containing 4nnp as an id)

Run:
python stage_for_chai.py colab_out_dir chai_folder

This should create the following:
    chai_folder/
        - 4nnp/
            - chai.fasta (input sequences for chai model)
            - msas/ (contain the same sequences + pairing as colabfold writes)
                - hash1.aligned.pqt
                - hash2.aligned.pqt
                - ...
            all_template_hits.m8 (contains template hits for all chains)

Then, you can Chai on the files:
chai-lab fold chai_folder/4nnp/chai.fasta 4nnp_out --msa-directory chai_folder/4nnp/msas/ --template-hits-path chai_folder/4nnp/all_template_hits.m8

NOTE This preserves the pairing that ColabFold determines; this is NOT necessarily
the same as the pairing that occurs when using the --use-msa-server flag.
"""

import logging
from pathlib import Path

import pandas as pd
import typer

from chai_lab.data.io.cif_utils import get_chain_letter
from chai_lab.data.parsing.fasta import Fasta, write_fastas
from chai_lab.data.parsing.msas.a3m import read_colabfold_a3m
from chai_lab.data.parsing.msas.aligned_pqt import (
    AlignedParquetModel,
    expected_basename,
)
from chai_lab.data.parsing.msas.data_source import MSADataSource
from chai_lab.data.parsing.templates.m8 import parse_m8_file

app = typer.Typer(pretty_exceptions_enable=False)


def read_colabfold_inputs(fname: Path) -> dict[str, list[Fasta]]:
    """Extracts sequences from colabfold input table."""
    df = pd.read_csv(fname, delimiter=",")
    assert list(df.columns) == ["id", "sequence"]
    retval: dict[str, list[Fasta]] = {}
    for row in df.itertuples():
        sequences: list[str] = row.sequence.split(":")  # type: ignore
        complex: list[Fasta] = [
            Fasta(header=f"protein|{get_chain_letter(i)}", sequence=seq)
            for i, seq in enumerate(sequences, start=1)
        ]
        retval[row.id] = complex  # type: ignore
    return retval


def gather_colabfold_msas(
    colabfold_out_dir: Path, identifier: str, output_folder: Path
) -> dict[str, str]:
    """Gathers MSAs generated by colabfold and writes them to the given output folder.

    Returns mapping of colabfold generated identifiers -> sequences.
    """
    output_folder.mkdir(parents=True, exist_ok=True)
    paired_msa = read_colabfold_a3m(
        colabfold_out_dir / f"{identifier}_pairgreedy/pair.a3m"
    )
    # The paired MSA should be the same number of rows for all
    paired_lengths = set(len(v) for v in paired_msa.values())
    assert len(paired_lengths) == 1
    n_paired = paired_lengths.pop()
    logging.info(f"[{identifier}] Colabfold paired {n_paired} MSAs")

    # Read in also the single chain MSAs
    uniref_msa = read_colabfold_a3m(colabfold_out_dir / f"{identifier}_env/uniref.a3m")

    env_msa = read_colabfold_a3m(
        colabfold_out_dir / f"{identifier}_env/bfd.mgnify30.metaeuk30.smag30.a3m"
    )
    assert set(uniref_msa.keys()) == set(env_msa.keys()) == set(paired_msa.keys())

    retval: dict[str, str] = {}
    for query in paired_msa.keys():
        query_seq = uniref_msa[query][0].sequence
        msa_rows = []
        for i, row in enumerate(paired_msa[query]):
            record = {
                "sequence": row.sequence,
                "source_database": (
                    MSADataSource.QUERY if i == 0 else MSADataSource.UNIREF90
                ).value,
                "pairing_key": str(i) if i > 0 else "",
                "comment": "null",
            }
            msa_rows.append(record)
        for row in uniref_msa[query][1:]:
            msa_rows.append(
                {
                    "sequence": row.sequence,
                    "source_database": MSADataSource.UNIREF90.value,
                    "pairing_key": "",
                    "comment": "null",
                }
            )
        for row in env_msa[query][1:]:
            msa_rows.append(
                {
                    "sequence": row.sequence,
                    "source_database": MSADataSource.BFD_UNICLUST.value,
                    "pairing_key": "",
                    "comment": "null",
                }
            )
        table = pd.DataFrame.from_records(msa_rows)
        AlignedParquetModel.validate(table)
        table.to_parquet(output_folder / expected_basename(query_sequence=query_seq))
        retval[query] = query_seq
    return retval


def gather_colabfold_templates(
    colabfold_out_dir: Path,
    identifier: str,
    chain_id_mapping: dict[str, str],
    output_folder: Path,
) -> Path:
    template_file = colabfold_out_dir / f"{identifier}_env" / "pdb70.m8"
    assert template_file.is_file()
    templates = parse_m8_file(template_file)
    templates["query_id"] = templates["query_id"].apply(
        lambda s: chain_id_mapping[str(s)]
    )
    outfile = output_folder / "all_template_hits.m8"
    templates.to_csv(outfile, sep="\t", index=False, header=False)
    return outfile


@app.command()
def main(colabfold_out_dir: Path, chai_dir: Path):
    """Takes a directory containing colabfold outputs and stages them for Chai1."""
    csv_files = list(colabfold_out_dir.glob("*.csv"))
    assert len(csv_files) == 1, f"Expected a single csv file but got {len(csv_files)}"
    fasta_entries: dict[str, list[Fasta]] = read_colabfold_inputs(csv_files.pop())

    for identifier, sequences in fasta_entries.items():
        chai_out_folder = chai_dir / identifier
        chai_out_folder.mkdir(parents=True, exist_ok=True)

        # Gather MSAs
        colabfold_id_to_seq = gather_colabfold_msas(
            colabfold_out_dir=colabfold_out_dir,
            identifier=identifier,
            output_folder=chai_out_folder / "msas",
        )
        assert set(colabfold_id_to_seq.values()) == set([f.sequence for f in sequences])

        # Build a mapping for each sequence in the input to the
        colab_id_to_chai_id = {}
        for colabfold_id, seq in colabfold_id_to_seq.items():
            chai_seq_matches = [s for s in sequences if s.sequence == seq]
            assert len(chai_seq_matches)
            colab_id_to_chai_id[colabfold_id] = chai_seq_matches.pop().header.split(
                "|", maxsplit=1
            )[-1]

        # Gather templates
        gather_colabfold_templates(
            colabfold_out_dir=colabfold_out_dir,
            identifier=identifier,
            chain_id_mapping=colab_id_to_chai_id,
            output_folder=chai_out_folder,
        )

        # Write the actual fasta input file
        write_fastas(sequences, (chai_out_folder / "chai.fasta").as_posix())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app()
