import unittest

from bs_scheduler import LambdaBS
from tests.test_utils import create_dataloader, iterate, simulate_n_epochs, fashion_mnist, \
    get_batch_sizes_across_epochs, BSTest, clip


class TestLambdaBS(BSTest):
    def setUp(self):
        self.base_batch_size = 64
        self.dataset = fashion_mnist()
        # TODO: Test multiple dataloaders: dataloader with workers, dataloaders with samplers, with drop last and
        #  without drop last and so on.

    @staticmethod
    def compute_expected_batch_sizes(epochs, base_batch_size, fn, min_batch_size, max_batch_size):
        return [base_batch_size] + [clip(int(base_batch_size * fn(epoch)), min_batch_size, max_batch_size) for
                                    epoch in range(1, epochs)]

    def test_sanity(self):
        dataloader = create_dataloader(self.dataset, batch_size=self.base_batch_size)
        real, inferred = iterate(dataloader)
        self.assert_real_eq_inferred(real, inferred)

        dataloader.batch_sampler.batch_size = 526
        real, inferred = iterate(dataloader)
        self.assert_real_eq_inferred(real, inferred)

    def test_dataloader_lengths(self):
        dataloader = create_dataloader(self.dataset, batch_size=self.base_batch_size)
        fn = lambda epoch: (1 + epoch) ** 1.05
        scheduler = LambdaBS(dataloader, fn)
        n_epochs = 300

        epoch_lengths = simulate_n_epochs(dataloader, scheduler, n_epochs)

        expected_batch_sizes = self.compute_expected_batch_sizes(n_epochs, self.base_batch_size, fn,
                                                                 scheduler.min_batch_size, scheduler.max_batch_size)
        expected_lengths = self.compute_epoch_lengths(expected_batch_sizes, len(self.dataset), drop_last=False)

        self.assert_real_eq_expected(epoch_lengths, expected_lengths)

    def test_dataloader_batch_size(self):
        dataloader = create_dataloader(self.dataset, batch_size=self.base_batch_size)
        fn = lambda epoch: 10 * epoch
        scheduler = LambdaBS(dataloader, fn)
        n_epochs = 15

        batch_sizes = get_batch_sizes_across_epochs(dataloader, scheduler, n_epochs)
        expected_batch_sizes = self.compute_expected_batch_sizes(n_epochs, self.base_batch_size, fn,
                                                                 scheduler.min_batch_size, scheduler.max_batch_size)
        self.assert_real_eq_expected(batch_sizes, expected_batch_sizes)


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    unittest.main()