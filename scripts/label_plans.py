"""Run the local floor-plan mask editor (no CAD connection required)."""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, default=REPO / "dataset" / "raw_233")
    parser.add_argument("--output", type=Path, default=REPO / "dataset" / "annotation_work")
    parser.add_argument("--training-output", type=Path, default=REPO / "dataset" / "annotated_cubicasa")
    parser.add_argument("--coco-output", type=Path, default=REPO / "dataset" / "annotated_coco")
    parser.add_argument("--pilot", type=Path, default=REPO / "dataset" / "pilot_30.json")
    survey = REPO / "dataset/survey/pilot_styles.csv"
    parser.add_argument("--survey", type=Path, default=survey if survey.exists() else REPO / "dataset/survey/survey.csv")
    parser.add_argument("--weights")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    import uvicorn
    from trace_engine.labeling import create_app
    app = create_app(args.images, args.output, args.survey, args.weights, args.training_output, args.coco_output, args.pilot)
    print(f"Plan Labeler: http://127.0.0.1:{args.port}", flush=True)
    print(f"COCO exports: {args.coco_output}", flush=True)
    print(f"Mask exports: {args.training_output}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
