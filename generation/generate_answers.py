import os
import sys
import json
import os.path as osp
from typing import Union, List

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM, GenerationConfig, BitsAndBytesConfig
from tqdm import tqdm

from tap import Tap

# Check if GPU is available
if torch.cuda.is_available():
    device = "cuda"
else:
    device = "cpu"

# Check if MPS is available
try:
    if torch.backends.mps.is_available():
        device = "mps"
except:  # noqa: E722
    pass


# Arguments class
class Arguments(Tap):
    ## Model parameters
    base_model: str = "aurora-m/Aurora-40k-hf"
    auth_token: str = ""

    ## Generation parameters
    max_new_tokens: int = 256
    num_beams: int = 4
    top_k: int = 40
    top_p: float = 0.75
    temperature: float = 0.1

    ## Input and output files
    prompt_template_path: str = "../../configs/alpaca.json"
    input_path: List[str] = ["data/test_input.json"]
    output_path: str = "../output/test_output.json"

    def configure(self):
        self.add_argument('--input_path', nargs='*')

# Prompter class
class Prompter(object):
    __slots__ = ("template", "_verbose")

    def __init__(self, template_name: str = "", verbose: bool = False):
        self._verbose = verbose
        if not template_name:
            # Enforce the default here, so the constructor can be called with '' and will not break.
            template_name = "alpaca"
        file_name = template_name  # osp.join("templates", f"{template_name}.json")
        if not osp.exists(file_name):
            raise ValueError(f"Can't read {file_name}")
        with open(file_name) as fp:
            self.template = json.load(fp)
        if self._verbose:
            print(
                f"Using prompt template {template_name}: {self.template['description']}"
            )

    def generate_prompt(
        self,
        instruction: str,
        input: Union[None, str] = None,
        label: Union[None, str] = None,
    ) -> str:
        # returns the full prompt from instruction and optional input
        # if a label (=response, =output) is provided, it's also appended.
        if input:
            res = self.template["prompt_input"].format(
                instruction=instruction, input=input
            )
        else:
            res = self.template["prompt_no_input"].format(instruction=instruction)
        if label:
            res = f"{res}{label}"
        if self._verbose:
            print(res)
        return res

    def get_response(self, output: str) -> str:
        return output.split(self.template["response_split"])[1].strip()


# Evaluation function
def evaluate(
    model,
    tokenizer,
    prompter,
    instruction,
    input=None,
    temperature=0.1,
    top_p=0.75,
    top_k=40,
    num_beams=4,
    max_new_tokens=128,
    stream_output=False,
    **kwargs,
):
    prompt = prompter.generate_prompt(instruction, input)
    inputs = tokenizer(prompt, return_tensors="pt")
    input_ids = inputs["input_ids"].to(device)
    generation_config = GenerationConfig(
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        num_beams=num_beams,
        **kwargs,
    )

    generate_params = {
        "input_ids": input_ids,
        "generation_config": generation_config,
        "return_dict_in_generate": True,
        "output_scores": True,
        "max_new_tokens": max_new_tokens,
    }

    # Without streaming
    with torch.no_grad():
        generation_output = model.generate(
            input_ids=input_ids,
            generation_config=generation_config,
            return_dict_in_generate=True,
            output_scores=True,
            max_new_tokens=max_new_tokens,
        )
    s = generation_output.sequences[0]
    output = tokenizer.decode(s, skip_special_tokens=True)
    return prompter.get_response(output)


# Main function
def main(args: Arguments):
    # Load the tokenizer and model
    tokenizer = AutoTokenizer.from_pretrained(args.base_model)

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=False,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16
    )

    model = AutoModelForCausalLM.from_pretrained(args.base_model, quantization_config=bnb_config,
                                                 low_cpu_mem_usage=True, device_map={"": 0})

    model = model.eval()

    model.eval()
    if torch.__version__ >= "2" and sys.platform != "win32":
        model = torch.compile(model)

    # Load the prompt template
    prompter = Prompter(args.prompt_template_path)

    # Load the input data (.json)
    input_path_list = args.input_path

    for input_path in input_path_list:
        with open(input_path) as f:
            input_data = json.load(f)

        # Save the outputs
        basename = os.path.basename(input_path)
        instructions = input_data["instructions"]
        inputs = input_data["inputs"]

        # Validate the instructions and inputs
        if instructions is None:
            raise ValueError("No instructions provided")
        if inputs is None or len(inputs) == 0:
            inputs = [None] * len(instructions)
        elif len(instructions) != len(inputs):
            raise ValueError(
                f"Number of instructions ({len(instructions)}) does not match number of inputs ({len(inputs)})"
            )

        # Generate the outputs
        outputs = []
        for instruction, input in tqdm(
            zip(instructions, inputs),
            total=len(instructions),
            desc=f"Evaluate {os.path.basename(args.base_model)} on {basename.split('.')[0]}",
        ):
            output = evaluate(
                model=model,
                tokenizer=tokenizer,
                prompter=prompter,
                instruction=instruction,
            )
            outputs.append(output)

        output_path = os.path.join(args.output_path, args.base_model, basename)
        # Check if the output path directory exists
        if not os.path.exists(os.path.dirname(output_path)):
            os.makedirs(os.path.dirname(output_path))
        # Save the outputs to the output path
        with open(output_path, "w") as f:
            json.dump(
                {
                    "parameters": {
                        "model": args.base_model,
                        "prompt_template": args.prompt_template_path,
                    },
                    "inputs": inputs,
                    "instructions": instructions,
                    "outputs": outputs,
                },
                f,
                indent=4,
            )


if __name__ == "__main__":
    args = Arguments().parse_args()
    main(args)
