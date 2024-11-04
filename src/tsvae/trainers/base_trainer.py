import contextlib
import datetime
import logging
import os
import os.path as pt
from copy import deepcopy
from typing import Any, Dict, List, Optional

import matplotlib.pyplot as plt
import torch
import torch.distributed as dist
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
import yaml
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from tsvae.dataset.base import BaseDataset, DatasetOutput, collate_dataset_output
from tsvae.dataset.data_pipeline import visualize_data_2d
from tsvae.models.base import BaseModel
from tsvae.models.utils.distances import GaussianMMD2
from tsvae.trainers.base_trainer_config import BaseTrainerConfig
from tsvae.trainers.training_callbacks import CallbackHandler, MetricConsolePrinterCallback, ProgressBarCallback, TrainingCallback
from tsvae.utils.logger_utils import get_console_logger
from tsvae.utils.random_utils import set_seed
from tsvae.utils.visualization_utils import visualize_data, visualize_real_recon_fake, visualize_real_recon_fake_2d

logger = get_console_logger(__name__)


class BaseTrainer:
    r"""
    This class is also from pythae with some modification to time series
    "https://github.com/clementchadebec/benchmark_VAE"

    Base class to perform model training.

    Args:
        model (BaseModel): A instance of :class:`~tsvae.models.base.BaseModel` to train

        train_dataset (BaseDataset): The training dataset of type
            :class:`~~tsvae.data.base.BaseModel`

        eval_dataset (BaseDataset): The evaluation dataset of type
            :class:`~pythae.data.dataset.BaseDataset`

        training_config (BaseTrainerConfig):

        callbacks (List[~tsvae.trainers.training_callbacks.TrainingCallbacks]):
            A list of callbacks to use during training.
    """

    def __init__(
        self,
        model: BaseModel,
        training_config: BaseTrainerConfig,
        train_dataset: BaseDataset,
        eval_dataset: Optional[BaseDataset] = None,
        callbacks: List[TrainingCallback] = None,
        exp_config=None,
        device_name=None,
    ):
        # Basic attributes
        self.exp_config = exp_config
        self.training_config = training_config
        self.model_config = model.model_config
        self.model_name = model.model_name

        # Attributes for distributed training
        self.world_size = self.training_config.world_size
        self.local_rank = self.training_config.local_rank
        self.rank = self.training_config.rank
        self.dist_backend = self.training_config.dist_backend

        if self.world_size > 1:
            self.distributed = True
        else:
            self.distributed = False

        # Set devices
        if self.distributed:
            device = self._setup_devices()
        else:
            device = "cuda" if torch.cuda.is_available() and not self.training_config.no_cuda else "cpu"
        if device_name is not None:
            device = device_name

        # Autocasting automatically chooses the precision for GPU
        self.amp_context = torch.autocast(device) if self.training_config.amp else contextlib.nullcontext()
        # Only for BCE cost
        if hasattr(model.model_config, "reconstruction_loss") and model.model_config.reconstruction_loss == "bce":
            self.amp_context = contextlib.nullcontext()

        self.device = device

        # Place model on device
        self.model = model.to(device)
        self.model.device = device  # Maybe should update generation instead of setting device for whole torch module

        if self.distributed:
            self.model = DDP(self.model, device_ids=[self.local_rank])

        self.train_dataset = train_dataset
        self.eval_dataset = eval_dataset

        # Define the dataloaders
        self.train_loader = self.get_train_dataloader(train_dataset)

        if eval_dataset is not None:
            self.eval_loader = self.get_eval_dataloader(eval_dataset)

        else:
            logger.info("! No eval dataset provided ! -> keeping best model on train.\n")
            self.training_config.keep_best_on_train = True

        self.callbacks = callbacks

        # run sanity check on the model
        self._run_model_sanity_check(self.model, self.train_loader)
        if self.training_config.ploter == "path":
            self.plot_func = visualize_data
            self.plot_compare = visualize_real_recon_fake
            logger.info("Path Visualization of data")
        elif self.training_config.ploter == "2d":
            self.plot_func = visualize_data_2d
            self.plot_compare = visualize_real_recon_fake_2d
            logger.info("2d Visualization of data")
        else:
            logger.info("NO SUCH VISUALIZATION FUNCTION")

        if self.is_main_process:
            logger.info("Model passed sanity check !\n" "Ready for training.\n")

    @property
    def is_main_process(self):
        if self.rank == 0 or self.rank == -1:
            return True
        else:
            return False

    def _set_inputs_to_device(self, inputs: Dict[str, Any]):

        inputs_on_device = inputs

        if "cuda" in self.device:
            cuda_inputs = dict.fromkeys(inputs)

            for key in inputs.keys():
                if torch.is_tensor(inputs[key]):
                    cuda_inputs[key] = inputs[key].to(self.device)

                else:
                    cuda_inputs[key] = inputs[key]
            inputs_on_device = cuda_inputs

        return inputs_on_device

    def _run_model_sanity_check(self, model, loader):
        try:
            inputs = next(iter(loader))
            train_dataset = self._set_inputs_to_device(inputs)
            model(train_dataset)

        except Exception as e:
            raise Exception(
                "Error when calling forward method from model. Potential issues: \n"
                " - Wrong model architecture -> check encoder, decoder and metric architecture if "
                "you provide yours \n"
                " - The data input dimension provided is wrong -> when no encoder, decoder or metric "
                "provided, a network is built automatically but requires the shape of the flatten "
                "input data.\n"
                f"Exception raised: {type(e)} with message: " + str(e)
            ) from e

    def _setup_devices(self):
        """Sets up the devices to perform distributed training."""

        if dist.is_available() and dist.is_initialized() and self.local_rank == -1:
            logger.warning("torch.distributed process group is initialized, but local_rank == -1. ")
        if self.training_config.no_cuda:
            self._n_gpus = 0
            device = "cpu"

        else:
            torch.cuda.set_device(self.local_rank)
            device = torch.device("cuda", self.local_rank)

            if not dist.is_initialized():
                dist.init_process_group(
                    backend=self.dist_backend,
                    init_method="env://",
                    world_size=self.world_size,
                    rank=self.rank,
                )

        return device

    def get_train_dataloader(self, train_dataset: BaseDataset) -> torch.utils.data.DataLoader:
        if self.distributed:
            train_sampler = DistributedSampler(train_dataset, num_replicas=self.world_size, rank=self.rank)
        else:
            train_sampler = None
        return DataLoader(
            dataset=train_dataset,
            batch_size=self.training_config.per_device_train_batch_size,
            num_workers=self.training_config.train_dataloader_num_workers,
            shuffle=(train_sampler is None),
            sampler=train_sampler,
            collate_fn=collate_dataset_output,
        )

    def get_eval_dataloader(self, eval_dataset: BaseDataset) -> torch.utils.data.DataLoader:
        if self.distributed:
            eval_sampler = DistributedSampler(eval_dataset, num_replicas=self.world_size, rank=self.rank)
        else:
            eval_sampler = None
        return DataLoader(
            dataset=eval_dataset,
            batch_size=self.training_config.per_device_eval_batch_size,
            num_workers=self.training_config.eval_dataloader_num_workers,
            shuffle=(eval_sampler is None),
            sampler=eval_sampler,
            collate_fn=collate_dataset_output,
        )

    def set_optimizer(self):
        optimizer_cls = getattr(optim, self.training_config.optimizer_cls)

        if self.training_config.optimizer_params is not None:
            optimizer = optimizer_cls(
                self.model.parameters(),
                lr=self.training_config.learning_rate,
                **self.training_config.optimizer_params,
            )
        else:
            optimizer = optimizer_cls(self.model.parameters(), lr=self.training_config.learning_rate)

        self.optimizer = optimizer

    def set_scheduler(self):
        if self.training_config.scheduler_cls is not None:
            scheduler_cls = getattr(lr_scheduler, self.training_config.scheduler_cls)

            if self.training_config.scheduler_params is not None:
                scheduler = scheduler_cls(self.optimizer, **self.training_config.scheduler_params)
            else:
                scheduler = scheduler_cls(self.optimizer)

        else:
            scheduler = None

        self.scheduler = scheduler

    def _set_output_dir(self):
        # Create folder
        if not os.path.exists(self.training_config.output_dir) and self.is_main_process:
            os.makedirs(self.training_config.output_dir, exist_ok=True)
            logger.info(f"Created {self.training_config.output_dir} folder since did not exist.\n")

        self._training_signature = str(datetime.datetime.now())[0:19].replace(" ", "_").replace(":", "-")

        training_dir = os.path.join(
            self.training_config.output_dir,
            f"{self.model_name}_training_{self._training_signature}",
        )

        self.training_dir = training_dir

        if not os.path.exists(training_dir) and self.is_main_process:
            os.makedirs(training_dir, exist_ok=True)
            logger.info(f"Created {training_dir}. \n" "Training config, checkpoints and final model will be saved here.\n")

    def _get_file_logger(self, log_output_dir):
        log_dir = log_output_dir

        # if dir does not exist create it
        if not os.path.exists(log_dir) and self.is_main_process:
            os.makedirs(log_dir, exist_ok=True)
            logger.info(f"Created {log_dir} folder since did not exists.")
            logger.info("Training logs will be recodered here.\n")
            logger.info(" -> Training can be monitored here.\n")

        # create and set logger
        log_name = f"training_logs_{self._training_signature}"

        file_logger = logging.getLogger(log_name)
        file_logger.setLevel(logging.INFO)
        f_handler = logging.FileHandler(os.path.join(log_dir, f"training_logs_{self._training_signature}.log"))
        f_handler.setLevel(logging.INFO)
        file_logger.addHandler(f_handler)

        # Do not output logs in the console
        file_logger.propagate = False

        return file_logger

    def _setup_callbacks(self):
        if self.callbacks is None:
            self.callbacks = [TrainingCallback()]

        self.callback_handler = CallbackHandler(callbacks=self.callbacks, model=self.model)

        self.callback_handler.add_callback(ProgressBarCallback())
        self.callback_handler.add_callback(MetricConsolePrinterCallback())

    def _set_optimizer_on_device(self, optim, device):
        for param in optim.state.values():
            # Not sure there are any global tensors in the state dict
            if isinstance(param, torch.Tensor):
                param.data = param.data.to(device)
                if param._grad is not None:
                    param._grad.data = param._grad.data.to(device)
            elif isinstance(param, dict):
                for subparam in param.values():
                    if isinstance(subparam, torch.Tensor):
                        subparam.data = subparam.data.to(device)
                        if subparam._grad is not None:
                            subparam._grad.data = subparam._grad.data.to(device)

        return optim

    def _optimizers_step(self, model_output=None):

        loss = model_output.loss

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

    def _schedulers_step(self, metrics=None):
        if self.scheduler is None:
            pass

        elif isinstance(self.scheduler, lr_scheduler.ReduceLROnPlateau):
            self.scheduler.step(metrics)

        else:
            self.scheduler.step()

    def _visualize_data_sanity_check(self, loader):
        inputs = next(iter(loader))
        fig = self.plot_func(inputs["data"])
        plt.savefig(pt.join(self.training_dir, "visualize_data_sanity.png"))
        plt.close(fig)

    def prepare_training(self):
        """Sets up the trainer for training"""
        # set random seed
        set_seed(self.training_config.seed)

        # set optimizer
        self.set_optimizer()

        # set scheduler
        self.set_scheduler()

        # create folder for saving
        self._set_output_dir()

        # set callbacks
        self._setup_callbacks()

    def train(self, log_output: bool):
        """This function is the main training function

        Args:
            log_output_dir (str): The path in which the log will be stored
        """

        self.prepare_training()

        config_file_path = pt.join(self.training_dir, "exp_config.yaml")
        with open(config_file_path, "w") as outfile:
            yaml.dump(self.exp_config, outfile, default_flow_style=False)

        self._visualize_data_sanity_check(self.train_loader)

        self.callback_handler.on_train_begin(training_config=self.training_config, model_config=self.model_config)

        log_verbose = False

        msg = (
            f"Training params:\n - max_epochs: {self.training_config.num_epochs}\n"
            " - per_device_train_batch_size: "
            f"{self.training_config.per_device_train_batch_size}\n"
            " - per_device_eval_batch_size: "
            f"{self.training_config.per_device_eval_batch_size}\n"
            f" - checkpoint saving every: {self.training_config.steps_saving}\n"
            f"Optimizer: {self.optimizer}\n"
            f"Scheduler: {self.scheduler}\n"
            f"Device: {self.device}\n"
        )

        if self.is_main_process:
            logger.info(msg)

        # set up log file
        if log_output is not None and self.is_main_process:
            log_verbose = True
            log_output_dir = self.training_dir
            self.file_logger = self._get_file_logger(log_output_dir=log_output_dir)
            self.file_logger.info("Start logging: ")
            self.file_logger.info(self.exp_config)
            self.file_logger.info(self.model)
            self.file_logger.info(self.training_config)
            self.file_logger.info(msg)

        if self.is_main_process:
            logger.info("Successfully launched training !\n")

        # set best losses for early stopping
        best_train_loss = 1e10
        best_eval_loss = 1e10

        for epoch in range(1, self.training_config.num_epochs + 1):

            self.callback_handler.on_epoch_begin(
                training_config=self.training_config,
                epoch=epoch,
            )

            metrics = {}

            epoch_train_loss = self.train_step(epoch)
            metrics["train_epoch_loss"] = epoch_train_loss

            if self.eval_dataset is not None:
                eval_metrics = self.eval_step(epoch)
                metrics.update(eval_metrics)
                epoch_eval_loss = best_eval_loss - 1
                self._schedulers_step(epoch_eval_loss)

            else:
                epoch_eval_loss = best_eval_loss
                self._schedulers_step(epoch_train_loss)

            if epoch_eval_loss < best_eval_loss and not self.training_config.keep_best_on_train:
                best_eval_loss = epoch_eval_loss
                best_model = deepcopy(self.model)
                self._best_model = best_model

            elif epoch_train_loss < best_train_loss and self.training_config.keep_best_on_train:
                best_train_loss = epoch_train_loss
                best_model = deepcopy(self.model)
                self._best_model = best_model

            if self.training_config.steps_predict is not None and epoch % self.training_config.steps_predict == 0 and self.is_main_process:
                fig = self.predict(best_model)

                self.callback_handler.on_prediction_step(
                    self.training_config,
                    fig=fig,
                    global_step=epoch,
                )
                plt.close(fig)

            self.callback_handler.on_epoch_end(training_config=self.training_config)

            # save checkpoints
            if self.training_config.steps_saving is not None and epoch % self.training_config.steps_saving == 0:
                if self.is_main_process:
                    self.save_checkpoint(model=best_model, dir_path=self.training_dir, epoch=epoch)
                    logger.info(f"Saved checkpoint at epoch {epoch}\n")

                    if log_verbose:
                        self.file_logger.info(f"Saved checkpoint at epoch {epoch}\n")

            self.callback_handler.on_log(
                self.training_config,
                metrics,
                logger=logger,
                global_step=epoch,
                rank=self.rank,
            )

        final_dir = os.path.join(self.training_dir, "final_model")

        if self.is_main_process:
            self.save_model(best_model, dir_path=final_dir)

            logger.info("Training ended!")
            logger.info(f"Saved final model in {final_dir}")

        if self.distributed:
            dist.destroy_process_group()

        self.callback_handler.on_train_end(self.training_config)

    # eval step and train step can combined together

    def eval_step(self, epoch: int):
        """Perform an evaluation step

        Parameters:
            epoch (int): The current epoch number

        Returns:
            (Dict): The evaluation loss
        """

        self.callback_handler.on_eval_step_begin(
            training_config=self.training_config,
            eval_loader=self.eval_loader,
            epoch=epoch,
            rank=self.rank,
        )

        self.model.eval()

        with self.amp_context:
            n_sample_test = 1000
            inputs = DatasetOutput(
                data=self.eval_dataset.data[:n_sample_test],
                labels=self.eval_dataset.labels[:n_sample_test],
            )
            inputs = self._set_inputs_to_device(inputs)
            with torch.no_grad():
                model_output = self.model(inputs)
                data_x = inputs["data"]
                recon_x = model_output["recon_x"]
                gen_x = self.model.generation(n_sample_test, c=inputs["labels"])
            recon_data = recon_x.to("cpu")
            real_data = data_x.to("cpu")
            fake_data = gen_x.to("cpu")
            eval_metrics = {}
            gaussian_mmd = GaussianMMD2()
            eval_metrics["eval_mmd_real_fake"] = gaussian_mmd(real_data, fake_data)
            eval_metrics["eval_mmd_real_recon"] = gaussian_mmd(real_data, recon_data)

            self.callback_handler.on_eval_step_end(training_config=self.training_config)

        return eval_metrics

    def train_step(self, epoch: int):
        """The trainer performs training loop over the train_loader.

        Parameters:
            epoch (int): The current epoch number

        Returns:
            (torch.Tensor): The step training loss
        """
        self.callback_handler.on_train_step_begin(
            training_config=self.training_config,
            train_loader=self.train_loader,
            epoch=epoch,
            rank=self.rank,
        )

        # set model in train model
        self.model.train()

        epoch_loss = 0

        for inputs in self.train_loader:

            inputs = self._set_inputs_to_device(inputs)

            with self.amp_context:
                model_output = self.model(
                    inputs,
                    epoch=epoch,
                    dataset_size=len(self.train_loader.dataset),
                    uses_ddp=self.distributed,
                )

            self._optimizers_step(model_output)

            loss = model_output.loss

            epoch_loss += loss.item()

            if epoch_loss != epoch_loss:  # TODO: update to other expression later
                raise ArithmeticError("NaN detected in train loss")

            self.callback_handler.on_train_step_end(training_config=self.training_config)

        logger.info(f"total loss: {model_output.loss.item():.2f}")
        if "recon_loss" in model_output:
            logger.info(f"recon: {model_output.recon_loss.item():.2f}")
        if "reg_loss" in model_output:
            logger.info(f"reg: {model_output.reg_loss.item():.2f}")
        if "mmd_loss" in model_output:
            logger.info(f"mmd: {model_output.mmd_loss.item():.2f}")

        # Allows model updates if needed
        if self.distributed:
            self.model.module.update()
        else:
            self.model.update()

        epoch_loss_mean = epoch_loss / len(self.train_loader)

        return epoch_loss_mean

    def save_model(self, model: BaseModel, dir_path: str):
        """This method saves the final model along with the config files

        Args:
            model (BaseModel): The model to be saved
            dir_path (str): The folder where the model and config files should be saved
        """

        if not os.path.exists(dir_path):
            os.makedirs(dir_path)

        # save model
        if self.distributed:
            model.module.save(dir_path)

        else:
            model.save(dir_path)

        # save training config
        self.training_config.save_json(dir_path, "training_config")

        self.callback_handler.on_save(self.training_config)

    def save_checkpoint(self, model: BaseModel, dir_path, epoch: int):
        """Saves a checkpoint alowing to restart training from here

        Args:
            dir_path (str): The folder where the checkpoint should be saved
            epochs_signature (int): The epoch number"""

        checkpoint_dir = os.path.join(dir_path, f"checkpoint_epoch_{epoch}")

        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)

        # save optimizer
        torch.save(
            deepcopy(self.optimizer.state_dict()),
            os.path.join(checkpoint_dir, "optimizer.pt"),
        )

        # save scheduler
        if self.scheduler is not None:
            torch.save(
                deepcopy(self.scheduler.state_dict()),
                os.path.join(checkpoint_dir, "scheduler.pt"),
            )

        # save model
        if self.distributed:
            model.module.save(checkpoint_dir)

        else:
            model.save(checkpoint_dir)

        # save training config
        self.training_config.save_json(checkpoint_dir, "training_config")

        # save visualization
        fig = self.predict(model)
        plt.savefig(os.path.join(checkpoint_dir, "real_recon_fake.png"))
        plt.close()

    def predict(self, model: BaseModel):
        # compare real recon fake

        model.eval()

        with self.amp_context:

            inputs = next(iter(self.train_loader))
            inputs = self._set_inputs_to_device(inputs)
            real = inputs["data"].cpu()
            n_gen = len(real)

            model_out = model(inputs)
            rec = model_out.recon_x.cpu().detach()[:n_gen]

            if self.distributed:
                fake = model.module.generation(n_gen, c=inputs["labels"]).detach().cpu()
            else:
                fake = model.generation(n_gen, c=inputs["labels"]).detach().cpu()

        fig = self.plot_compare(real, rec, fake)

        return fig
