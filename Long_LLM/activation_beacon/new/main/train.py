import logging
from transformers import HfArgumentParser
from transformers.integrations import is_deepspeed_zero3_enabled
from src import ( 
    Data,
    DefaultDataCollator,
    ModelArgs,
    TrainingArgs,
    FileLogger,
    get_model_and_tokenizer,
    makedirs
)
from src.trainer import ActivationBeaconTrainer
from src.metrics import Metric

logger = logging.getLogger(__name__)


def main():
    parser = HfArgumentParser([ModelArgs, TrainingArgs])
    model_args, training_args = parser.parse_args_into_dataclasses()
    
    model, tokenizer = get_model_and_tokenizer(model_args)

    if model_args.enable_beacon:
        for name, param in model.named_parameters():
            if "beacon" not in name:
                param.requires_grad_(False)

    if training_args.lora_tune:
        from peft import (
            LoraConfig,
            get_peft_model,
        )
        # copied from LongLoRA
        config = LoraConfig(
            r=training_args.lora_rank,
            lora_alpha=training_args.lora_alpha,
            target_modules=training_args.lora_targets,
            modules_to_save=training_args.lora_extra_params,
            lora_dropout=training_args.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(model, config)

    if training_args.pretrain_config is None:
        with training_args.main_process_first():
            train_dataset = Data.prepare_train_data(
                model_args.train_data, 
                tokenizer=tokenizer,
                max_length=model_args.max_length,
                min_length=training_args.min_length,
                chat_template=model_args.chat_template,
                max_train_num_per_data=training_args.max_train_num_per_data,
                seed=training_args.seed,
                retrieval_tuning=training_args.retrieval_tuning,
                beacon_window=model_args.beacon_window,
                cache_dir=model_args.dataset_cache_dir,
            )

    else:
        train_dataset = Data.prepare_pretrain_data(
            model_args.train_data, 
            tokenizer=tokenizer,
            config=training_args.pretrain_config,
            seed=training_args.seed,
            cache_dir=model_args.dataset_cache_dir,
            main_process_first_context=training_args.main_process_first,
            is_main_process=training_args.process_index == 0,
        )

    with training_args.main_process_first():
        if is_deepspeed_zero3_enabled() and training_args.eval_method != "perplexity":
            logger.warning(f"In deepspeed zero3, evaluation with generation is may lead to hang because of the unequal number of forward passes across different devices.")
        eval_dataset = Data.prepare_eval_data(
            model_args.eval_data, 
            tokenizer=tokenizer,
            max_length=training_args.eval_max_length,
            min_length=training_args.eval_min_length,
            chat_template=model_args.chat_template,
            seed=training_args.seed,
            cache_dir=model_args.dataset_cache_dir,
        )

    trainer = ActivationBeaconTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        model_args=model_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=DefaultDataCollator(tokenizer),
        file_logger=FileLogger(makedirs(training_args.log_path)),
        compute_metrics=Metric.get_metric_fn(
            metrics=training_args.metrics,
            save_path=Metric.get_save_path(
                model_args.eval_data,
                training_args.output_dir
            ) if model_args.eval_data is not None else None
        )
    )
    if train_dataset is not None:
        trainer.train()
    elif eval_dataset is not None:
        trainer.evaluate()

if __name__ == "__main__":
    main()
