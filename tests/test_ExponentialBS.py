import unittest

from bs_scheduler import ExponentialBS
from tests.test_utils import create_dataloader, simulate_n_epochs, fashion_mnist, \
    get_batch_sizes_across_epochs, BSTest


class TestExponentialBS(BSTest):
    def setUp(self):
        self.base_batch_size = 64
        self.dataset = fashion_mnist()
        # TODO: Test multiple dataloaders: dataloader with workers, dataloaders with samplers, with drop last and
        #  without drop last and so on.

    def test_dataloader_lengths(self):
        dataloader = create_dataloader(self.dataset, batch_size=self.base_batch_size)

        scheduler = ExponentialBS(dataloader, gamma=1.1)
        n_epochs = 5

        epoch_lengths = simulate_n_epochs(dataloader, scheduler, n_epochs)
        expected_batch_sizes = [64, 70, 77, 85, 94]
        expected_lengths = self.compute_epoch_lengths(expected_batch_sizes, len(self.dataset), drop_last=False)
        self.assertEqual(epoch_lengths, expected_lengths)

    def test_dataloader_batch_size(self):
        base_batch_size = 10
        dataloader = create_dataloader(self.dataset, batch_size=base_batch_size)
        scheduler = ExponentialBS(dataloader, gamma=2, max_batch_size=100, verbose=False)
        n_epochs = 10

        batch_sizes = get_batch_sizes_across_epochs(dataloader, scheduler, n_epochs)
        expected_batch_sizes = [10, 20, 40, 80] + [100] * 6

        self.assertEqual(batch_sizes, expected_batch_sizes)


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    unittest.main()
