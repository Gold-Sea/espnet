#!/usr/bin/env python3

"""TTS mode decoding."""

import argparse
import logging
from pathlib import Path
import shutil
import sys
import time
from typing import Any
from typing import Dict
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union

import matplotlib
import numpy as np
import soundfile as sf
import torch
from typeguard import check_argument_types

from espnet.utils.cli_utils import get_commandline_args
from espnet2.fileio.npy_scp import NpyScpWriter
from espnet2.gan_tts.vits import VITS
from espnet2.tasks.tts import TTSTask
from espnet2.torch_utils.device_funcs import to_device
from espnet2.torch_utils.set_all_random_seed import set_all_random_seed
from espnet2.tts.fastspeech import FastSpeech
from espnet2.tts.fastspeech2 import FastSpeech2
from espnet2.tts.tacotron2 import Tacotron2
from espnet2.tts.transformer import Transformer
from espnet2.tts.utils.duration_calculator import DurationCalculator
from espnet2.utils import config_argparse
from espnet2.utils.get_default_kwargs import get_default_kwargs
from espnet2.utils.griffin_lim import Spectrogram2Waveform
from espnet2.utils.nested_dict_action import NestedDictAction
from espnet2.utils.types import str2bool
from espnet2.utils.types import str2triple_str
from espnet2.utils.types import str_or_none


class Text2Speech:
    """Text2Speech module.

    Examples:
        >>> import soundfile
        >>> text2speech = Text2Speech("config.yml", "model.pth")
        >>> wav = text2speech("Hello World")["wav"]
        >>> soundfile.write("out.wav", wav.numpy(), text2speech.fs, "PCM_16")

    """

    def __init__(
        self,
        train_config: Optional[Union[Path, str]],
        model_file: Optional[Union[Path, str]] = None,
        threshold: float = 0.5,
        minlenratio: float = 0.0,
        maxlenratio: float = 10.0,
        use_teacher_forcing: bool = False,
        use_att_constraint: bool = False,
        backward_window: int = 1,
        forward_window: int = 3,
        speed_control_alpha: float = 1.0,
        noise_scale: float = 0.667,
        noise_scale_dur: float = 0.8,
        vocoder_conf: dict = None,
        dtype: str = "float32",
        device: str = "cpu",
        seed: int = 777,
        always_fix_seed: bool = False,
    ):
        """Initialize Text2Speech module."""
        assert check_argument_types()

        # setup model
        model, train_args = TTSTask.build_model_from_file(
            train_config, model_file, device
        )
        model.to(dtype=getattr(torch, dtype)).eval()
        self.device = device
        self.dtype = dtype
        self.train_args = train_args
        self.model = model
        self.tts = model.tts
        self.normalize = model.normalize
        self.feats_extract = model.feats_extract
        self.duration_calculator = DurationCalculator()
        self.preprocess_fn = TTSTask.build_preprocess_fn(train_args, False)
        self.use_teacher_forcing = use_teacher_forcing
        self.seed = seed
        self.always_fix_seed = always_fix_seed
        logging.info(f"Extractor:\n{self.feats_extract}")
        logging.info(f"Normalizer:\n{self.normalize}")
        logging.info(f"TTS:\n{self.tts}")

        # setup decoding config
        decode_config = {}
        decode_config.update(use_teacher_forcing=use_teacher_forcing)
        if isinstance(self.tts, (Tacotron2, Transformer)):
            decode_config.update(
                threshold=threshold,
                maxlenratio=maxlenratio,
                minlenratio=minlenratio,
            )
        if isinstance(self.tts, Tacotron2):
            decode_config.update(
                use_att_constraint=use_att_constraint,
                forward_window=forward_window,
                backward_window=backward_window,
            )
        if isinstance(self.tts, (FastSpeech, FastSpeech2, VITS)):
            decode_config.update(alpha=speed_control_alpha)
        if isinstance(self.tts, VITS):
            decode_config.update(
                noise_scale=noise_scale,
                noise_scale_dur=noise_scale_dur,
            )
        self.decode_config = decode_config

        # setup vocoder
        if vocoder_conf is None:
            vocoder_conf = {}
        if self.feats_extract is not None:
            vocoder_conf.update(self.feats_extract.get_parameters())
        if self.tts.require_vocoder and (
            "n_fft" in vocoder_conf
            and "n_shift" in vocoder_conf
            and "fs" in vocoder_conf
        ):
            self.spc2wav = Spectrogram2Waveform(**vocoder_conf)
            logging.info(f"Vocoder: {self.spc2wav}")
        else:
            self.spc2wav = None
            logging.info("Vocoder is not used.")

    @torch.no_grad()
    def __call__(
        self,
        text: Union[str, torch.Tensor, np.ndarray],
        speech: Union[torch.Tensor, np.ndarray] = None,
        durations: Union[torch.Tensor, np.ndarray] = None,
        spembs: Union[torch.Tensor, np.ndarray] = None,
        sids: Union[torch.Tensor, np.ndarray] = None,
        lids: Union[torch.Tensor, np.ndarray] = None,
        decode_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, torch.Tensor]:
        """Run text-to-speech."""
        assert check_argument_types()

        # check inputs
        if self.use_speech and speech is None:
            raise RuntimeError("missing required argument: 'speech'")

        # prepare batch
        if isinstance(text, str):
            text = self.preprocess_fn("<dummy>", dict(text=text))["text"]
        batch = dict(text=text)
        if speech is not None:
            batch.update(speech=speech)
        if durations is not None:
            batch.update(durations=durations)
        if spembs is not None:
            batch.update(spembs=spembs)
        if sids is not None:
            batch.update(sids=sids)
        if lids is not None:
            batch.update(lids=lids)
        batch = to_device(batch, self.device)

        # overwrite the decode configs if provided
        cfg = self.decode_config.copy()
        if decode_config is not None:
            cfg.update(decode_config)

        # inference
        if self.always_fix_seed:
            set_all_random_seed(self.seed)
        output_dict = self.model.inference(**batch, **cfg)

        # calculate additional metrics
        if output_dict.get("att_w") is not None:
            duration, focus_rate = self.duration_calculator(output_dict["att_w"])
            output_dict.update(duration=duration, focus_rate=focus_rate)

        # apply vocoder (mel-to-wav)
        if self.spc2wav is not None:
            if output_dict.get("feat_gen_denorm") is not None:
                input_feat = output_dict["feat_gen_denorm"]
            else:
                input_feat = output_dict["feat_gen"]
            wav = torch.tensor(self.spc2wav(input_feat.cpu().numpy()))
            output_dict.update(wav=wav)

        return output_dict

    @property
    def fs(self) -> Optional[int]:
        """Return sampling rate."""
        if self.spc2wav is not None:
            return self.spc2wav.fs
        elif hasattr(self.tts, "fs"):
            return self.tts.fs
        else:
            return None

    @property
    def use_speech(self) -> bool:
        """Return speech is needed or not in the inference."""
        return self.use_teacher_forcing or getattr(self.tts, "use_gst", False)


def inference(
    output_dir: str,
    batch_size: int,
    dtype: str,
    ngpu: int,
    seed: int,
    num_workers: int,
    log_level: Union[int, str],
    data_path_and_name_and_type: Sequence[Tuple[str, str, str]],
    key_file: Optional[str],
    train_config: Optional[str],
    model_file: Optional[str],
    threshold: float,
    minlenratio: float,
    maxlenratio: float,
    use_teacher_forcing: bool,
    use_att_constraint: bool,
    backward_window: int,
    forward_window: int,
    speed_control_alpha: float,
    noise_scale: float,
    noise_scale_dur: float,
    always_fix_seed: bool,
    allow_variable_data_keys: bool,
    vocoder_conf: dict,
):
    """Run text-to-speech inference."""
    assert check_argument_types()
    if batch_size > 1:
        raise NotImplementedError("batch decoding is not implemented")
    if ngpu > 1:
        raise NotImplementedError("only single GPU decoding is supported")
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s",
    )

    if ngpu >= 1:
        device = "cuda"
    else:
        device = "cpu"

    # 1. Set random-seed
    set_all_random_seed(seed)

    # 2. Build model
    text2speech = Text2Speech(
        train_config=train_config,
        model_file=model_file,
        threshold=threshold,
        maxlenratio=maxlenratio,
        minlenratio=minlenratio,
        use_teacher_forcing=use_teacher_forcing,
        use_att_constraint=use_att_constraint,
        backward_window=backward_window,
        forward_window=forward_window,
        speed_control_alpha=speed_control_alpha,
        noise_scale=noise_scale,
        noise_scale_dur=noise_scale_dur,
        vocoder_conf=vocoder_conf,
        dtype=dtype,
        device=device,
        seed=seed,
        always_fix_seed=always_fix_seed,
    )

    # 3. Build data-iterator
    if not text2speech.use_speech:
        data_path_and_name_and_type = list(
            filter(lambda x: x[1] != "speech", data_path_and_name_and_type)
        )
    loader = TTSTask.build_streaming_iterator(
        data_path_and_name_and_type,
        dtype=dtype,
        batch_size=batch_size,
        key_file=key_file,
        num_workers=num_workers,
        preprocess_fn=TTSTask.build_preprocess_fn(text2speech.train_args, False),
        collate_fn=TTSTask.build_collate_fn(text2speech.train_args, False),
        allow_variable_data_keys=allow_variable_data_keys,
        inference=True,
    )

    # 6. Start for-loop
    output_dir = Path(output_dir)
    (output_dir / "norm").mkdir(parents=True, exist_ok=True)
    (output_dir / "denorm").mkdir(parents=True, exist_ok=True)
    (output_dir / "speech_shape").mkdir(parents=True, exist_ok=True)
    (output_dir / "wav").mkdir(parents=True, exist_ok=True)
    (output_dir / "att_ws").mkdir(parents=True, exist_ok=True)
    (output_dir / "probs").mkdir(parents=True, exist_ok=True)
    (output_dir / "durations").mkdir(parents=True, exist_ok=True)
    (output_dir / "focus_rates").mkdir(parents=True, exist_ok=True)

    # Lazy load to avoid the backend error
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    with NpyScpWriter(
        output_dir / "norm",
        output_dir / "norm/feats.scp",
    ) as norm_writer, NpyScpWriter(
        output_dir / "denorm", output_dir / "denorm/feats.scp"
    ) as denorm_writer, open(
        output_dir / "speech_shape/speech_shape", "w"
    ) as shape_writer, open(
        output_dir / "durations/durations", "w"
    ) as duration_writer, open(
        output_dir / "focus_rates/focus_rates", "w"
    ) as focus_rate_writer:
        for idx, (keys, batch) in enumerate(loader, 1):
            assert isinstance(batch, dict), type(batch)
            assert all(isinstance(s, str) for s in keys), keys
            _bs = len(next(iter(batch.values())))
            assert _bs == 1, _bs

            # Change to single sequence and remove *_length
            # because inference() requires 1-seq, not mini-batch.
            batch = {k: v[0] for k, v in batch.items() if not k.endswith("_lengths")}

            start_time = time.perf_counter()
            output_dict = text2speech(**batch)

            key = keys[0]
            insize = next(iter(batch.values())).size(0) + 1
            if output_dict.get("feat_gen") is not None:
                # standard text2mel model case
                feat_gen = output_dict["feat_gen"]
                logging.info(
                    "inference speed = {:.1f} frames / sec.".format(
                        int(feat_gen.size(0)) / (time.perf_counter() - start_time)
                    )
                )
                logging.info(f"{key} (size:{insize}->{feat_gen.size(0)})")
                if feat_gen.size(0) == insize * maxlenratio:
                    logging.warning(f"output length reaches maximum length ({key}).")

                norm_writer[key] = output_dict["feat_gen"].cpu().numpy()
                shape_writer.write(
                    f"{key} " + ",".join(map(str, output_dict["feat_gen"].shape)) + "\n"
                )
                if output_dict.get("feat_gen_denorm") is not None:
                    denorm_writer[key] = output_dict["feat_gen_denorm"].cpu().numpy()
            else:
                # end-to-end text2wav model case
                wav = output_dict["wav"]
                logging.info(
                    "inference speed = {:.1f} points / sec.".format(
                        int(wav.size(0)) / (time.perf_counter() - start_time)
                    )
                )
                logging.info(f"{key} (size:{insize}->{wav.size(0)})")

            if output_dict.get("duration") is not None:
                # Save duration and fucus rates
                duration_writer.write(
                    f"{key} "
                    + " ".join(map(str, output_dict["duration"].long().cpu().numpy()))
                    + "\n"
                )

            if output_dict.get("focus_rate") is not None:
                focus_rate_writer.write(
                    f"{key} {float(output_dict['focus_rate']):.5f}\n"
                )

            if output_dict.get("att_w") is not None:
                # Plot attention weight
                att_w = output_dict["att_w"].cpu().numpy()

                if att_w.ndim == 2:
                    att_w = att_w[None][None]
                elif att_w.ndim != 4:
                    raise RuntimeError(f"Must be 2 or 4 dimension: {att_w.ndim}")

                w, h = plt.figaspect(att_w.shape[0] / att_w.shape[1])
                fig = plt.Figure(
                    figsize=(
                        w * 1.3 * min(att_w.shape[0], 2.5),
                        h * 1.3 * min(att_w.shape[1], 2.5),
                    )
                )
                fig.suptitle(f"{key}")
                axes = fig.subplots(att_w.shape[0], att_w.shape[1])
                if len(att_w) == 1:
                    axes = [[axes]]
                for ax, att_w in zip(axes, att_w):
                    for ax_, att_w_ in zip(ax, att_w):
                        ax_.imshow(att_w_.astype(np.float32), aspect="auto")
                        ax_.set_xlabel("Input")
                        ax_.set_ylabel("Output")
                        ax_.xaxis.set_major_locator(MaxNLocator(integer=True))
                        ax_.yaxis.set_major_locator(MaxNLocator(integer=True))

                fig.set_tight_layout({"rect": [0, 0.03, 1, 0.95]})
                fig.savefig(output_dir / f"att_ws/{key}.png")
                fig.clf()

            if output_dict.get("prob") is not None:
                # Plot stop token prediction
                prob = output_dict["prob"].cpu().numpy()

                fig = plt.Figure()
                ax = fig.add_subplot(1, 1, 1)
                ax.plot(prob)
                ax.set_title(f"{key}")
                ax.set_xlabel("Output")
                ax.set_ylabel("Stop probability")
                ax.set_ylim(0, 1)
                ax.grid(which="both")

                fig.set_tight_layout(True)
                fig.savefig(output_dir / f"probs/{key}.png")
                fig.clf()

            if output_dict.get("wav") is not None:
                # TODO(kamo): Write scp
                sf.write(
                    f"{output_dir}/wav/{key}.wav",
                    output_dict["wav"].cpu().numpy(),
                    text2speech.fs,
                    "PCM_16",
                )

    # remove files if those are not included in output dict
    if output_dict.get("feat_gen") is None:
        shutil.rmtree(output_dir / "norm")
    if output_dict.get("feat_gen_denorm") is None:
        shutil.rmtree(output_dir / "denorm")
    if output_dict.get("att_w") is None:
        shutil.rmtree(output_dir / "att_ws")
    if output_dict.get("duration") is None:
        shutil.rmtree(output_dir / "durations")
    if output_dict.get("focus_rate") is None:
        shutil.rmtree(output_dir / "focus_rates")
    if output_dict.get("prob") is None:
        shutil.rmtree(output_dir / "probs")
    if output_dict.get("wav") is None:
        shutil.rmtree(output_dir / "wav")


def get_parser():
    """Get argument parser."""
    parser = config_argparse.ArgumentParser(
        description="TTS Decode",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Note(kamo): Use "_" instead of "-" as separator.
    # "-" is confusing if written in yaml.
    parser.add_argument(
        "--log_level",
        type=lambda x: x.upper(),
        default="INFO",
        choices=("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"),
        help="The verbose level of logging",
    )

    parser.add_argument(
        "--output_dir",
        type=str,
        required=True,
        help="The path of output directory",
    )
    parser.add_argument(
        "--ngpu",
        type=int,
        default=0,
        help="The number of gpus. 0 indicates CPU mode",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed",
    )
    parser.add_argument(
        "--dtype",
        default="float32",
        choices=["float16", "float32", "float64"],
        help="Data type",
    )
    parser.add_argument(
        "--num_workers",
        type=int,
        default=1,
        help="The number of workers used for DataLoader",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=1,
        help="The batch size for inference",
    )

    group = parser.add_argument_group("Input data related")
    group.add_argument(
        "--data_path_and_name_and_type",
        type=str2triple_str,
        required=True,
        action="append",
    )
    group.add_argument(
        "--key_file",
        type=str_or_none,
    )
    group.add_argument(
        "--allow_variable_data_keys",
        type=str2bool,
        default=False,
    )

    group = parser.add_argument_group("The model configuration related")
    group.add_argument(
        "--train_config",
        type=str,
        help="Training configuration file.",
    )
    group.add_argument(
        "--model_file",
        type=str,
        help="Model parameter file.",
    )

    group = parser.add_argument_group("Decoding related")
    group.add_argument(
        "--maxlenratio",
        type=float,
        default=10.0,
        help="Maximum length ratio in decoding",
    )
    group.add_argument(
        "--minlenratio",
        type=float,
        default=0.0,
        help="Minimum length ratio in decoding",
    )
    group.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Threshold value in decoding",
    )
    group.add_argument(
        "--use_att_constraint",
        type=str2bool,
        default=False,
        help="Whether to use attention constraint",
    )
    group.add_argument(
        "--backward_window",
        type=int,
        default=1,
        help="Backward window value in attention constraint",
    )
    group.add_argument(
        "--forward_window",
        type=int,
        default=3,
        help="Forward window value in attention constraint",
    )
    group.add_argument(
        "--use_teacher_forcing",
        type=str2bool,
        default=False,
        help="Whether to use teacher forcing",
    )
    parser.add_argument(
        "--speed_control_alpha",
        type=float,
        default=1.0,
        help="Alpha in FastSpeech to change the speed of generated speech",
    )
    parser.add_argument(
        "--noise_scale",
        type=float,
        default=0.667,
        help="Noise scale parameter for the flow in vits",
    )
    parser.add_argument(
        "--noise_scale_dur",
        type=float,
        default=0.8,
        help="Noise scale parameter for the stochastic duration predictor in vits",
    )
    group.add_argument(
        "--always_fix_seed",
        type=str2bool,
        default=False,
        help="Whether to always fix seed",
    )

    group = parser.add_argument_group("Grriffin-Lim related")
    group.add_argument(
        "--vocoder_conf",
        action=NestedDictAction,
        default=get_default_kwargs(Spectrogram2Waveform),
        help="The configuration for Grriffin-Lim",
    )
    return parser


def main(cmd=None):
    """Run TTS model decoding."""
    print(get_commandline_args(), file=sys.stderr)
    parser = get_parser()
    args = parser.parse_args(cmd)
    kwargs = vars(args)
    kwargs.pop("config", None)
    inference(**kwargs)


if __name__ == "__main__":
    main()
