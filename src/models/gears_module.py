import numpy as np

from typing import Any, Dict, Tuple

import torch
from lightning import LightningModule
from torch_geometric.data import Batch
from torchmetrics import MaxMetric, MeanMetric
from torchmetrics.regression import SpearmanCorrCoef

from .components.gears import GEARSNetwork
from gears.utils import loss_fct
from gears import PertData


class GEARSLitModule(LightningModule):
    """LightningModule wrapper for GEARS.

    A `LightningModule` implements 8 key methods:

    ```python
    def __init__(self):
    # Define initialization code here.

    def setup(self, stage):
    # Things to setup before each stage, 'fit', 'validate', 'test', 'predict'.
    # This hook is called on every process when using DDP.

    def training_step(self, batch, batch_idx):
    # The complete training step.

    def validation_step(self, batch, batch_idx):
    # The complete validation step.

    def test_step(self, batch, batch_idx):
    # The complete test step.

    def predict_step(self, batch, batch_idx):
    # The complete predict step.

    def configure_optimizers(self):
    # Define and configure optimizers and LR schedulers.
    ```

    Docs:
        https://lightning.ai/docs/pytorch/latest/common/lightning_module.html
    """
    def __init__(
                self,
                net: GEARSNetwork,
                pertmodule: PertData,
                optimizer: Any,
                scheduler: Any,
                compile: bool = False
        ) -> None:
        """Initialize a `GEARSLitModule`.

        :param net: The model to train.
        """
        super().__init__()

        # this line allows to access init params with 'self.hparams' attribute
        # also ensures init params will be stored in ckpt
        self.save_hyperparameters(logger=False)

        self.net = net

        # loss function
        self.criterion = loss_fct

        adata = pertmodule.pert_data.adata
        pert_full_id2pert = dict(adata.obs[['condition_name', 'condition']].values)
        self.dict_filter = {pert_full_id2pert[i]: j for i, j in adata.uns['non_zeros_gene_idx'].items()
                            if i in pert_full_id2pert}

        self.pert_list = pertmodule.pert_list

        self.ctrl_expression = torch.tensor(np.mean(adata.X[adata.obs.condition == 'ctrl'], axis=0)).reshape(-1, )

        # metric objects for calculating and averaging accuracy across batches
        num_genes = pertmodule.num_genes
        self.train_spr = SpearmanCorrCoef(num_outputs=num_genes)
        self.val_spr = SpearmanCorrCoef(num_outputs=num_genes)
        self.test_spr = SpearmanCorrCoef(num_outputs=num_genes)

        # for averaging loss across batches
        self.train_loss = MeanMetric()
        self.val_loss = MeanMetric()
        self.test_loss = MeanMetric()

        # for tracking best so far validation accuracy
        self.val_spr_best = MaxMetric()

        self.net.model_initialize(pertmodule)

    def forward(self, batch: Batch) -> torch.Tensor:
        """Perform a forward pass through the model `self.net`.

        :param batch: A PyG Batch object containing a subgraph passed to the model for training.
        :return: A tensor of gene-level RNA expression.
        """
        return self.net(batch)

    def on_train_start(self) -> None:
        """Lightning hook that is called when training begins."""
        # by default lightning executes validation step sanity checks before training starts,
        # so it's worth to make sure validation metrics don't store results from these checks
        self.val_loss.reset()
        self.val_spr.reset()
        self.val_spr_best.reset()

    def model_step(
            self, batch: Batch
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Perform a single model step on a batch of data.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target labels.

        :return: A tuple containing (in order):
            - A tensor of losses.
            - A tensor of predictions.
            - A tensor of target labels.
        """
        y = batch.y

        dir_lambda = self.net.direction_lambda
        preds = self.forward(batch)

        loss = self.criterion(preds, y, perts=batch.pert, ctrl=self.ctrl_expression, dict_filter=self.dict_filter,
                              direction_lambda=dir_lambda)
        return loss, preds, y

    def training_step(
            self, batch: Batch, batch_idx: int
    ) -> torch.Tensor:
        """Perform a single training step on a batch of data from the training set.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target
            labels.
        :param batch_idx: The index of the current batch.
        :return: A tensor of losses between model predictions and targets.
        """
        loss, preds, targets = self.model_step(batch)
        # update and log metrics
        self.train_loss(loss)
        train_spr = self.train_spr(preds, targets)
        avg_train_spr = torch.mean(train_spr)
        self.log("train/loss", self.train_loss, on_step=True, on_epoch=True, prog_bar=True)
        self.log("train/avg_spearman", avg_train_spr, on_step=False, on_epoch=True, prog_bar=True)

        # return loss or backpropagation will fail
        return loss

    def on_train_epoch_end(self) -> None:
        """Lightning hook that is called when a training epoch ends."""
        pass

    def validation_step(self, batch: Batch, batch_idx: int) -> None:
        """Perform a single validation step on a batch of data from the validation set.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target
            labels.
        :param batch_idx: The index of the current batch.
        """
        loss, preds, targets = self.model_step(batch)
        # update and log metrics
        self.val_loss(loss)
        val_spr = self.val_spr(preds, targets)
        avg_val_spr = torch.mean(val_spr)
        self.log("val/loss", self.val_loss, on_step=False, on_epoch=True, prog_bar=True)
        self.log("val/avg_spearman", avg_val_spr, on_step=False, on_epoch=True, prog_bar=True)

    def on_validation_epoch_end(self) -> None:
        """Lightning hook that is called when a validation epoch ends."""
        spr = self.val_spr.compute()
        avg_val_spr = torch.mean(spr)
        self.val_spr_best(avg_val_spr)
        self.log("val/avg_spr_best", self.val_spr_best.compute(), sync_dist=True, prog_bar=True)

    def test_step(self, batch: Batch, batch_idx: int) -> None:
        """Perform a single test step on a batch of data from the test set.

        :param batch: A batch of data (a tuple) containing the input tensor of images and target
            labels.
        :param batch_idx: The index of the current batch.
        """
        loss, preds, targets = self.model_step(batch)

        # update and log metrics
        self.test_loss(loss)
        test_spr = self.test_spr(preds, targets)
        avg_test_spr = torch.mean(test_spr)
        self.log("test/loss", self.test_loss, on_step=False, on_epoch=True, prog_bar=True)
        self.log("test/spr", avg_test_spr, on_step=False, on_epoch=True, prog_bar=True)

    def on_test_epoch_end(self) -> None:
        """Lightning hook that is called when a test epoch ends."""
        pass

    def setup(self, stage: str) -> None:
        """Lightning hook that is called at the beginning of fit (train + validate), validate,
        test, or predict.

        This is a good hook when you need to build models dynamically or adjust something about
        them. This hook is called on every process when using DDP.

        :param stage: Either `"fit"`, `"validate"`, `"test"`, or `"predict"`.
        """
        if self.hparams.compile and stage == "fit":
            self.net = torch.compile(self.net)

    def configure_optimizers(self) -> Dict[str, Any]:
        """Choose what optimizers and learning-rate schedulers to use in your optimization.
        Normally you'd need one. But in the case of GANs or similar you might have multiple.

        Examples:
            https://lightning.ai/docs/pytorch/latest/common/lightning_module.html#configure-optimizers

        :return: A dict containing the configured optimizers and learning-rate schedulers to be used for training.
        """
        optimizer = self.hparams.optimizer(params=self.trainer.model.parameters())
        if self.hparams.scheduler is not None:
            scheduler = self.hparams.scheduler(optimizer=optimizer)
            return {
                "optimizer": optimizer,
                "lr_scheduler": {
                    "scheduler": scheduler,
                    "monitor": "val/loss",
                    "interval": "epoch",
                    "frequency": 1,
                },
            }
        return {"optimizer": optimizer}


if __name__ == "__main__":
    _ = GEARSLitModule(None, None)