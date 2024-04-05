"""This module contains the main function for querying a task.

It generates the predicate columns, builds the tree, and recursively queries the tree.
"""

from pathlib import Path
from datetime import timedelta

import polars as pl
from bigtree import preorder_iter, print_tree

from EventStream.data.dataset_polars import Dataset

from .config import build_tree_from_config, load_config
from .event_predicates import generate_predicate_columns
from .query import query_subtree


def query_task(cfg_path: str, data: str | pl.DataFrame, verbose=True) -> pl.DataFrame:
    """Query a task using the provided configuration file and event stream data.

    Args:
        cfg_path: The path to the configuration file.
        ESD: The event stream data.

    Returns:
        polars.DataFrame: The result of the task query.
    """
    match data:
        case str():
            DATA_DIR = Path(data)
            ESD = Dataset.load(DATA_DIR)

            events_df = ESD.events_df.filter(~pl.all_horizontal(pl.all().is_null()))
            dynamic_measurements_df = ESD.dynamic_measurements_df.filter(
                ~pl.all_horizontal(pl.all().is_null())
            )

            ESD_data = (
                events_df.join(dynamic_measurements_df, on="event_id", how="left")
                .drop(["event_id"])
                .sort(by=["subject_id", "timestamp", "event_type"])
            )
        case pl.DataFrame():
            ESD_data = data

    if ESD_data["timestamp"].dtype != pl.Datetime:
        ESD_data = ESD_data.with_columns(
            pl.col("timestamp")
            .str.strptime(pl.Datetime, format="%m/%d/%Y %H:%M")
            .cast(pl.Datetime)
        )

    if ESD_data.shape[0] == 0:
        raise ValueError("Empty ESD!")
    if "timestamp" not in ESD_data.columns:
        raise ValueError("ESD does not have timestamp column!")
    if "subject_id" not in ESD_data.columns:
        raise ValueError("ESD does not have subject_id column!")

    if verbose:
        print("Loading config...\n")
    cfg = load_config(cfg_path)

    if verbose:
        print("Generating predicate columns...\n")
    try:
        ESD_data = generate_predicate_columns(cfg, ESD_data, verbose=verbose)
    except Exception as e:
        raise ValueError(
            "Error generating predicate columns from configuration file! Check to make sure the format of "
            "the configuration file is valid."
        ) from e

    if verbose:
        print("\nBuilding tree...")
    tree = build_tree_from_config(cfg)
    if verbose:
        print_tree(tree, style="const_bold")
        print("\n")

    predicate_cols = [col for col in ESD_data.columns if col.startswith("is_")]

    if cfg.windows.trigger.includes:
        valid_trigger_exprs = [
            (ESD_data[f"is_{x['predicate']}"] == 1) for x in cfg.windows.trigger.includes
        ]
    else:
        valid_trigger_exprs = [
            (ESD_data[f"is_{cfg.windows.trigger.start}"] == 1)
        ]
    anchor_to_subtree_root_by_subtree_anchor = ESD_data.clone()
    anchor_to_subtree_root_by_subtree_anchor_shape = (
        anchor_to_subtree_root_by_subtree_anchor.shape[0]
    )
    for i, condition in enumerate(valid_trigger_exprs):
        dropped = anchor_to_subtree_root_by_subtree_anchor.filter(~condition)
        anchor_to_subtree_root_by_subtree_anchor = (
            anchor_to_subtree_root_by_subtree_anchor.filter(condition)
        )
        if (
            anchor_to_subtree_root_by_subtree_anchor.shape[0]
            < anchor_to_subtree_root_by_subtree_anchor_shape
        ):
            if verbose:
                if cfg.windows.trigger.includes:
                    print(
                        f"{dropped['subject_id'].unique().shape[0]} subjects ({dropped.shape[0]} rows) were excluded due to trigger condition: {cfg.windows.trigger.includes[i]}."
                    )
                else:
                    print(
                        f"{dropped['subject_id'].unique().shape[0]} subjects ({dropped.shape[0]} rows) were excluded due to trigger event: {cfg.windows.trigger.start}."
                    )
            anchor_to_subtree_root_by_subtree_anchor_shape = (
                anchor_to_subtree_root_by_subtree_anchor.shape[0]
            )

    anchor_to_subtree_root_by_subtree_anchor = (
        anchor_to_subtree_root_by_subtree_anchor.select(
            "subject_id", "timestamp", *[pl.col(c) for c in predicate_cols]
        ).with_columns(
            "subject_id", "timestamp", *[pl.lit(0).alias(c) for c in predicate_cols]
        )
    )

    if verbose:
        print("\n")
        print("Querying...")
    result = query_subtree(
        subtree=tree,
        anchor_to_subtree_root_by_subtree_anchor=anchor_to_subtree_root_by_subtree_anchor,
        predicates_df=ESD_data,
        anchor_offset=timedelta(hours=0),
        verbose=verbose
    )
    if verbose:
        print("\n")
        print("Done.\n")

    output_order = [node for node in preorder_iter(tree)]

    result = result.select(
        "subject_id",
        "timestamp",
        *[f"{c.name}/timestamp" for c in output_order[1:]],
        *[f"{c.name}/window_summary" for c in output_order[1:]],
    ).rename({"timestamp": f"{tree.name}/timestamp"})
    
    label_window = None
    for window in cfg.windows:
        if "label" in cfg.windows[window]:
            if cfg.windows[window].label:
                label_window = window
                break

    if label_window:
        label = cfg.windows[label_window].label
        result = result.with_columns(
            pl.col(f"{label_window}/window_summary")
            .struct.field(f"is_{label}")
            .alias("label")
        )
    return result
