"""QLoRA fine-tuning — GPU-optimized for RTX 4060 8GB.
Uses 8-bit quantization (more stable than 4-bit on CUDA 12.x).
"""
import argparse, os, sys
from pathlib import Path
import torch
sys.stdout.reconfigure(line_buffering=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-path", default=None)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--lora-dir", default=None)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--val-data-path", default=None, help="Validation JSONL path")
    args = parser.parse_args()

    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    data_path = Path(args.data_path) if args.data_path else PROJECT_ROOT / "data" / "sft_data.jsonl"
    model_dir = Path(args.model_dir) if args.model_dir else PROJECT_ROOT / "models" / "qwen2.5-coder-3b-instruct"
    lora_dir = Path(args.lora_dir) if args.lora_dir else PROJECT_ROOT / "models" / "qwen2.5-coder-3b-repomind-lora"
    model_dir.mkdir(parents=True, exist_ok=True)
    lora_dir.mkdir(parents=True, exist_ok=True)

    print(f"CUDA: {torch.cuda.is_available()} | GPU: {torch.cuda.get_device_name() if torch.cuda.is_available() else 'N/A'}")
    print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")

    from datasets import load_dataset
    from transformers import (AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig,
                              TrainingArguments, Trainer, DataCollatorForLanguageModeling)
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

    dataset = load_dataset("json", data_files=str(data_path), split="train")
    print(f"Data: {len(dataset)} entries")

    model_name = "Qwen/Qwen2.5-Coder-3B-Instruct"
    tokenizer = AutoTokenizer.from_pretrained(model_name, cache_dir=str(model_dir), trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Tokenize with shorter max_length
    def tokenize(examples):
        texts = []
        for instr, inp, out in zip(examples["instruction"], examples["input"], examples["output"]):
            text = f"<|im_start|>system\n{instr}<|im_end|>\n<|im_start|>user\n{inp}<|im_end|>\n<|im_start|>assistant\n{out}<|im_end|>"
            texts.append(text)
        return tokenizer(texts, truncation=True, max_length=512)
    dataset = dataset.map(tokenize, batched=True, remove_columns=dataset.column_names)
    print(f"Tokenized: {len(dataset)} samples")

    val_dataset = None
    if args.val_data_path:
        val_dataset = load_dataset("json", data_files=args.val_data_path, split="train")
        val_dataset = val_dataset.map(tokenize, batched=True, remove_columns=val_dataset.column_names)
        print(f"Val: {len(val_dataset)} entries")

    # 8-bit quant (more stable on CUDA 12.x than 4-bit)
    bnb_config = BitsAndBytesConfig(
        load_in_8bit=True,
        llm_int8_threshold=6.0,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_name, cache_dir=str(model_dir),
        quantization_config=bnb_config,
        device_map={"": 0}, torch_dtype=torch.float16, trust_remote_code=True,
    )
    model = prepare_model_for_kbit_training(model)
    model.gradient_checkpointing_enable()

    lora_config = LoraConfig(r=8, lora_alpha=16, target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
                             lora_dropout=0, bias="none", task_type="CAUSAL_LM")
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

    training_args = TrainingArguments(
        output_dir=str(lora_dir), num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size, gradient_accumulation_steps=8,
        warmup_steps=5, logging_steps=5, save_steps=50, save_total_limit=1,
        learning_rate=args.lr, lr_scheduler_type="cosine",
        fp16=True, optim="adamw_8bit",
        weight_decay=0.01, report_to="none", remove_unused_columns=False,
        evaluation_strategy="epoch", logging_strategy="epoch",
    )

    trainer = Trainer(model=model, args=training_args, train_dataset=dataset, eval_dataset=val_dataset, data_collator=data_collator)
    print(f"Training {len(dataset)//(args.batch_size*8)*args.epochs} steps...")
    trainer.train()

    if val_dataset is not None:
        val_metrics = trainer.evaluate()
        print(f"Train loss: {trainer.state.log_history[-2].get('loss', 0.0):.4f}  |  Val loss: {val_metrics['eval_loss']:.4f}  |  Gap: {val_metrics['eval_loss'] - trainer.state.log_history[-2].get('loss', 0.0):.4f}")

    print(f"Saving LoRA to {lora_dir}")
    model.save_pretrained(str(lora_dir))
    tokenizer.save_pretrained(str(lora_dir))

    merged = model.merge_and_unload()
    merged_dir = lora_dir / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(str(merged_dir))
    tokenizer.save_pretrained(str(merged_dir))
    print(f"Done: {merged_dir}")

if __name__ == "__main__":
    main()
