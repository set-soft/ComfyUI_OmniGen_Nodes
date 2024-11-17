import folder_paths
from huggingface_hub import snapshot_download
import logging
import numpy as np
import os
from PIL import Image
import sys
import torch
from torchvision import transforms

sys.path.append(os.path.dirname(__file__))
from .omnigen_wrappers import OmniGenProcessorWrapper, OmniGenPipelineWrapper
from .OmniGen import OmniGenPipeline
from .OmniGen.utils import show_shape, crop_arr, NEGATIVE_PROMPT

model_path = os.path.join(folder_paths.models_dir, "OmniGen", "Shitao", "OmniGen-v1")
r1 = [[0, 1], [1, 0]]
g1 = [[1, 0], [0, 1]]
b1 = [[1, 1], [0, 0]]
EMPTY_IMG = torch.tensor([r1, g1, b1]).unsqueeze(0)


def tensor2pil(t_image: torch.Tensor)  -> Image:
    return Image.fromarray(np.clip(255.0 * t_image.cpu().numpy().squeeze(), 0, 255).astype(np.uint8))


class OmniGen_Model:
    def __init__(self, quantization):
        self.quantization = quantization
        self.pipe = OmniGenPipeline.from_pretrained(model_path, Quantization=quantization)


def validate_image(idx, image, prompt, max_input_image_size):
    """ Ensure is used in the prompt, replace by the real marker and resize to a multiple of 16 """
    # Replace {image_N}, optionaly image_N, stop if not in prompt
    img_txt = f"image_{idx}"
    img_txt_curly = "{"+img_txt+"}"
    img_marker = f"<img><|image_{idx}|></img>"
    if img_txt_curly in prompt:
        prompt = prompt.replace(img_txt_curly, img_marker)
    else:
        assert img_txt in prompt, f"Image slot {idx} used, but the image isn't mentioned in the prompt"
        prompt = prompt.replace(img_txt, img_marker)
    # Make the image size usable [B,H,W,C]
    w = image.size(-2)
    h = image.size(-3)
    if w<128 or h<128 or w>max_input_image_size or h>max_input_image_size or w%16 or h%16:
        # Ok, the image needs size adjust
        img = tensor2pil(image)
        img = crop_arr(img, max_input_image_size)
        to_tens = transforms.ToTensor()  # [C,H,W]
        image = to_tens(img).unsqueeze(0).movedim(1, -1)
        logging.info(f"Rescaling image {idx} from {w}x{h} to {image.size(-2)}x{image.size(-3)}")
        logging.debug(image.shape)
    return image, prompt


class DZ_OmniGenV1:

    def __init__(self):
        self.NODE_NAME = "OmniGen Wrapper"
        self.model = None

    @classmethod
    def INPUT_TYPES(s):
        dtype_list = ["default", "int8"]
        return {
            "required": {
                "dtype": (dtype_list,),
                "prompt": ("STRING", {
                    "default": "input image as {image_1}, e.g.", "multiline":True, "defaultInput": True
                }),
                "vae": ("VAE",),
                "width": ("INT", {
                    "default": 512, "min": 16, "max": 2048, "step": 16
                }),
                "height": ("INT", {
                    "default": 512, "min": 16, "max": 2048, "step": 16
                }),
                "guidance_scale": ("FLOAT", {
                    "default": 2.5, "min": 1.0, "max": 5.0, "step": 0.1
                }),
                "img_guidance_scale": ("FLOAT", {
                    "default": 1.6, "min": 1.0, "max": 2.0, "step": 0.1
                }),
                "steps": ("INT", {
                    "default": 25, "min": 1, "max": 100, "step": 1
                }),
                "separate_cfg_infer": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Can save memory when generating images of large size at the expense of slower inference"
                }),
                "use_kv_cache": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Enable kv cache to speed up the inference"
                }),
                "seed": ("INT", {
                    "default": 0, "min": 0, "max": 1e18, "step": 1
                }),
                "cache_model": ("BOOLEAN", {
                    "default": True, "tooltip": "Cache model in V/RAM to save loading time"
                }),
                "move_to_ram": ("BOOLEAN", {
                    "default": True, "tooltip": "Keep in VRAM only the needed models. Move to main RAM the rest"
                }),
                "max_input_image_size": ("INT", {
                    "default": 1024, "min": 256, "max": 2048, "step": 16
                }),
            },
            "optional": {
                "image_1": ("IMAGE",),
                "image_2": ("IMAGE",),
                "image_3": ("IMAGE",),
                "negative": ("STRING", {"default": "", "placeholder": "Negative", "multiline": True, "defaultInput": True}),
            }
        }

    RETURN_TYPES = ("LATENT","IMAGE","IMAGE","IMAGE",)
    RETURN_NAMES = ("latent", "crp_img_1", "crp_img_2", "crp_img_3")
    FUNCTION = "run_omnigen"
    CATEGORY = 'OmniGen'

    def run_omnigen(self, dtype, prompt, vae, width, height, guidance_scale, img_guidance_scale,
                    steps, separate_cfg_infer, use_kv_cache, seed, cache_model, move_to_ram, max_input_image_size,
                    image_1=None, image_2=None, image_3=None, negative=None
                 ):

        input_images = []
        if image_1 is not None:
            crp_img_1, prompt = validate_image(1, image_1, prompt, max_input_image_size)
            input_images.append(crp_img_1)
        else:
            crp_img_1 = EMPTY_IMG
        if image_2 is not None:
            assert image_1 is not None, "Don't use image slot 2 if slot 1 is empty"
            crp_img_2, prompt = validate_image(2, image_2, prompt, max_input_image_size)
            input_images.append(crp_img_2)
        else:
            crp_img_2 = EMPTY_IMG
        if image_3 is not None:
            assert image_2 is not None, "Don't use image slot 3 if slot 2 is empty"
            crp_img_3, prompt = validate_image(3, image_3, prompt, max_input_image_size)
            input_images.append(crp_img_3)
        else:
            crp_img_3 = EMPTY_IMG
        if len(input_images) == 0:
            input_images = None

        if not os.path.exists(os.path.join(model_path, "model.safetensors")):
            snapshot_download("Shitao/OmniGen-v1",local_dir=model_path)

        quantization = True if dtype == "int8" else False
        if self.model is None or self.model.quantization != quantization:
            self.model = OmniGen_Model(quantization)

        # Generate image
        output = self.model.pipe(
            prompt=prompt,
            negative_prompt=negative,
            input_images=input_images,
            height=height,
            width=width,
            guidance_scale=guidance_scale,
            img_guidance_scale=img_guidance_scale,
            num_inference_steps=steps,
            separate_cfg_infer=separate_cfg_infer,  # set False can speed up the inference process
            use_kv_cache=use_kv_cache,
            seed=seed,
            move_to_ram=move_to_ram,
            max_input_image_size=max_input_image_size,
            vae = vae,
        )

        if not cache_model:
            self.model = None
            import gc
            # Cleanup
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()

        return ({'samples': output}, crp_img_1, crp_img_2, crp_img_3,)


class OmniGenConditioner:
    def __init__(self):
        self.NODE_NAME = "OmniGen Conditioner"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "prompt": ("STRING", {
                    "default": "input image as {image_1}, e.g.", "multiline":True, "defaultInput": True
                }),
                "max_input_image_size": ("INT", {
                    "default": 1024, "min": 256, "max": 2048, "step": 16
                }),
            },
            "optional": {
                "image_1": ("IMAGE",),
                "image_2": ("IMAGE",),
                "image_3": ("IMAGE",),
                "negative": ("STRING", {"default": "", "placeholder": "Negative", "multiline": True, "defaultInput": True}),
            }
        }

    RETURN_TYPES = ("OMNI_COND", "IMAGE", "IMAGE", "IMAGE")
    RETURN_NAMES = ("conditioner", "crp_img_1", "crp_img_2", "crp_img_3")
    FUNCTION = "run"
    CATEGORY = 'OmniGen'

    def run(self, prompt, max_input_image_size, image_1=None, image_2=None, image_3=None, negative=None):

        input_images = []
        if image_1 is not None:
            crp_img_1, prompt = validate_image(1, image_1, prompt, max_input_image_size)
            input_images.append(crp_img_1)
        else:
            crp_img_1 = EMPTY_IMG
        if image_2 is not None:
            assert image_1 is not None, "Don't use image slot 2 if slot 1 is empty"
            crp_img_2, prompt = validate_image(2, image_2, prompt, max_input_image_size)
            input_images.append(crp_img_2)
        else:
            crp_img_2 = EMPTY_IMG
        if image_3 is not None:
            assert image_2 is not None, "Don't use image slot 3 if slot 2 is empty"
            crp_img_3, prompt = validate_image(3, image_3, prompt, max_input_image_size)
            input_images.append(crp_img_3)
        else:
            crp_img_3 = EMPTY_IMG
        if len(input_images) == 0:
            input_images = None

        if negative is None:
            negative = NEGATIVE_PROMPT

        return ({'positive': prompt, 'negative': negative, 'images': input_images}, crp_img_1, crp_img_2, crp_img_3,)


class OmniGenProcessor:
    def __init__(self):
        self.NODE_NAME = "OmniGen Processor"
        self.processor = None

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "condition_1": ("OMNI_COND",),
                "separate_cfg_infer": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Can save memory when generating images of large size at the expense of slower inference"
                }),
                "size_from_first_image": ("BOOLEAN", {
                    "default": True, "tooltip": "Output size will be the same of the first image"
                }),
                "width": ("INT", {
                    "default": 512, "min": 16, "max": 2048, "step": 16,
                    "tooltip": "Width of the output image, unless size_from_first_image is enabled",
                }),
                "height": ("INT", {
                    "default": 512, "min": 16, "max": 2048, "step": 16,
                    "tooltip": "Height of the output image, unless size_from_first_image is enabled",
                }),
            },
            "optional": {
                "condition_2": ("OMNI_COND",),
                "condition_3": ("OMNI_COND",),
            }
        }

    RETURN_TYPES = ("OMNI_FULL_COND",)
    RETURN_NAMES = ("conditioner", )
    FUNCTION = "run"
    CATEGORY = 'OmniGen'

    def run(self, condition_1, separate_cfg_infer, size_from_first_image, width, height, condition_2=None, condition_3=None):
        positive = [condition_1['positive']]
        negative = [condition_1['negative']]
        images = [condition_1['images']]

        if condition_2 is not None:
            positive.append(condition_2['positive'])
            negative.append(condition_2['negative'])
            images.append(condition_2['images'])

        if condition_3 is not None:
            positive.append(condition_3['positive'])
            negative.append(condition_3['negative'])
            images.append(condition_3['images'])

        found_images = False
        final_images = []
        for img in images:
            if img is None:
                final_images.append([])
            else:
                found_images = True
                final_images.append(img)
        if not found_images:
            final_images = None

        if size_from_first_image:
            assert final_images is not None, "Asking to use the size of the first image, but no images provided"
            for imgs in final_images:
                if len(imgs):
                    img = imgs[0]
                    break
            # Images are in Comfy_UI format [B,H,W,C]
            width = img.size(-2)
            height = img.size(-3)

        if not self.processor:
            self.processor = OmniGenProcessorWrapper.from_pretrained()

        input_data = self.processor(positive, final_images, height=height, width=width, use_img_cfg=final_images is not None,
                                    separate_cfg_input=separate_cfg_infer, negative_prompt=negative)

        input_data['separate_cfg_infer'] = separate_cfg_infer
        input_data['input_images'] = final_images
        input_data['num_conditions'] = len(positive)
        input_data['height'] = height
        input_data['width'] = width
        return (input_data,)


class OmniGenSampler:
    def __init__(self):
        self.NODE_NAME = "OmniGen Sampler"
        self.pipe = None

    @classmethod
    def INPUT_TYPES(s):
        dtype_list = ["default", "int8"]
        return {
            "required": {
                "conditioner": ("OMNI_FULL_COND",),
                "dtype": (dtype_list,),
                "vae": ("VAE",),
                "guidance_scale": ("FLOAT", {
                    "default": 2.5, "min": 1.0, "max": 5.0, "step": 0.1
                }),
                "img_guidance_scale": ("FLOAT", {
                    "default": 1.6, "min": 1.0, "max": 2.0, "step": 0.1
                }),
                "steps": ("INT", {
                    "default": 25, "min": 1, "max": 100, "step": 1
                }),
                "use_kv_cache": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Enable kv cache to speed up the inference"
                }),
                "seed": ("INT", {
                    "default": 0, "min": 0, "max": 1e18, "step": 1
                }),
                "cache_model": ("BOOLEAN", {
                    "default": True, "tooltip": "Cache model in V/RAM to save loading time"
                }),
                "move_to_ram": ("BOOLEAN", {
                    "default": True, "tooltip": "Keep in VRAM only the needed models. Move to main RAM the rest"
                }),
            },
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "run"
    CATEGORY = 'OmniGen'

    def run(self, conditioner, dtype, vae, guidance_scale, img_guidance_scale, steps, use_kv_cache, seed, cache_model,
            move_to_ram):

        if not os.path.exists(os.path.join(model_path, "model.safetensors")):
            snapshot_download("Shitao/OmniGen-v1", local_dir=model_path)

        quantization = True if dtype == "int8" else False
        if self.pipe is None or self.pipe.quantization != quantization:
            self.pipe = OmniGenPipelineWrapper.from_pretrained(model_path, quantization)

        # Generate image
        output = self.pipe(conditioner,
                           num_inference_steps=steps,
                           guidance_scale=guidance_scale,
                           img_guidance_scale=img_guidance_scale,
                           use_kv_cache=use_kv_cache,
                           seed=seed,
                           move_to_ram=move_to_ram,
                           vae = vae,)

        if not cache_model:
            self.pipe = None
            import gc
            # Cleanup
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()

        return ({'samples': output},)


NODE_CLASS_MAPPINGS = {
    "dzOmniGenWrapper": DZ_OmniGenV1,
    "setOmniGenConditioner": OmniGenConditioner,
    "setOmniGenProcessor": OmniGenProcessor,
    "setOmniGenSampler": OmniGenSampler,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "dzOmniGenWrapper": "😺dz: OmniGen Wrapper",
    "setOmniGenConditioner": "OmniGen Conditioner (set)",
    "setOmniGenProcessor": "OmniGen Processor (set)",
    "setOmniGenSampler": "OmniGen Sampler (set)",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
