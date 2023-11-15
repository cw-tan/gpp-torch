import numpy as np
import torch
from linear_operator.operators import IdentityLinearOperator, DiagLinearOperator, LinearOperator, TriangularLinearOperator, ConstantDiagLinearOperator, CholLinearOperator
from linear_operator import settings

import time
from linear_operator.utils import stable_qr


# Notes:
# 1. linear operator Cholesky decomposition seems slower than native torch on cpu


def kernel(x1, x2, noise=1, l=1, diag=False):
      """
      x1 (torch.Tensor) : d × m tensor where m is the number of x vectors and d is the
                          dimensionality of the x vectors
      x2 (torch.Tensor) : d × n tensor n where is the number of x vectors and d is the
                          dimensionality of the x vectors
      """
      assert torch.is_tensor(x1) and torch.is_tensor(x2), 'x1 and x2 must be torch.Tensor'
      assert 0 < len(x1.shape) <= 2 and 0 < len(x2.shape) <= 2, 'x1 and x2 must be 1D or 2D'
      x1_mat = torch.atleast_2d(x1)
      x2_mat = torch.atleast_2d(x2)
      assert x1_mat.shape[0] == x2_mat.shape[0], 'vector dimensions of x1 and x2 are incompatible'

      if not diag:
          x1_mat = torch.transpose(x1_mat.unsqueeze(2).expand(x1_mat.shape + (x2_mat.shape[1],)), 0, 1)  # [m, d, n]
      else:
          assert x1_mat.shape[1] == x2_mat.shape[1], 'diag = True requires same dims'
      # the only thing different for other kernels is the last two lines
      scalar_form = torch.sum((x1_mat - x2_mat).pow(2), dim=1)  # [m, n]
      # for stability during hyperparameter optimization
      noise = torch.clamp(noise, min=1e-8)
      return noise * noise * torch.exp(-0.5 * scalar_form / (l * l))


class SparseGaussianProcess():
    """
    TODO: device manipulation
    """
    def __init__(self, descriptor_dim, invert_mode='QR'):
        """
        
        """
        self.descriptor_dim = descriptor_dim
        self.full_descriptors = torch.empty((descriptor_dim, 0))
        self.sparse_descriptors = torch.empty((descriptor_dim, 0))
        self.training_outputs = torch.empty((0,))

        # hyperparameters
        self.kernel_noise = torch.tensor([1], dtype=torch.float64)
        self.kernel_length = torch.tensor([1], dtype=torch.float64)
        self.kernel_hyperparameters = torch.tensor([1, 1], dtype=torch.float64)
        self.model_noise = torch.tensor([0.01], dtype=torch.float64)
        
        self.optimizer = torch.optim.Rprop([self.model_noise, self.kernel_noise, self.kernel_length])

        self.invert_mode = invert_mode
        # trained model parameters (precomputed during model updates for faster prediction)
        self.Ksf = None
        self.Lambda_inv = None
        self.Kss = None
        self.Sigma = None
        self.alpha = None  # for predicting mean

    def update_model(self, x_train, y_train, x_sparse):
        """
        Updates GP model with a set of training vectors, i.e. update the Cholesky decomposed L

        Args:
          x_train (torch.Tensor): d × m tensor where m is the number of x vectors and d is the
                                  dimensionality of the x descriptor vectors
          y_train (torch.Tensor): m-dimensional vector of training outputs corresponding to
                                  the training inputs x_train
        """
        assert torch.atleast_2d(x_train).shape[0] == self.descriptor_dim,\
          'x_train\'s dimensions are incompatible with the descriptor dimensions of the model'
        self.sparse_descriptors = torch.cat((self.sparse_descriptors, torch.atleast_2d(x_sparse)), dim=1)
        self.full_descriptors = torch.cat((self.full_descriptors, torch.atleast_2d(x_train)), dim=1)
        self.training_outputs = torch.cat((self.training_outputs, y_train))
        self.__update_Sigma_alpha()

    def __update_Sigma_alpha(self):
        Kff = kernel(self.full_descriptors, self.full_descriptors,
                     self.kernel_noise, self.kernel_length)
        Kss = kernel(self.sparse_descriptors, self.sparse_descriptors,
                     self.kernel_noise, self.kernel_length)
        self.Ksf = kernel(self.sparse_descriptors, self.full_descriptors,
                          self.kernel_noise, self.kernel_length)
        noise = torch.clamp(self.model_noise, min=1e-8)  # to avoid NaNs during hyperparameter optimization
        self.Lambda_inv = ConstantDiagLinearOperator(noise.pow(-2), self.full_descriptors.shape[1])

        Kss = Kss + torch.eye(self.sparse_descriptors.shape[1]) * 1e-8
 
        start = time.time()
        Lss = torch.linalg.cholesky(Kss)  # M³/3 flops
        end = time.time()
        #print('Cholesky factorize Kss: {:.5g}s'.format(end - start))

        #print('cond(Lss) = {:.4g}'.format(torch.linalg.cond(Lss).item()))

        # keep Kss in terms of its Cholesky decomposition (useful for enforcing matrix symmetry later)
        # results cached once an inversion is called on Kss or its Cholesky factors later on
        # TODO: decide whether to just do an inversion here to cache it or do lazy evaluations 
        # (likely an arbitrary choice in terms of performance, but may be important for future code readability)
        self.Kss = CholLinearOperator(Lss, upper=False)
 
        invert_start = time.time()
        match self.invert_mode:
            case 'N':  # use 'Normal Equations' method, i.e. direct Cholesky inverse (very unstable)
                Sigma_inv = Kss + self.Ksf @ self.Lambda_inv @ self.Ksf.T
                Sigma_inv = Sigma_inv + torch.eye(self.sparse_descriptors.shape[1]) * 1  # large jitter!

                #start = time.time()
                L_Sigma_inv = torch.linalg.cholesky(Sigma_inv)
                #end = time.time()
                #print('N method | Cholesky factorize: {:.5g}s'.format(end - start))

                #start = time.time()
                self.U_Sigma = TriangularLinearOperator(L_Sigma_inv, upper=False).inverse().T
                #end = time.time()
                #print('N method | Cholesky inverse: {:.5g}s'.format(end - start))

                #print('N method | cond(L_Σ_inv) = {:.4g}'.format(torch.linalg.cond(L_Sigma_inv).item()))
            case 'V':  # V method (relatively stable)
                #start = time.time()
                Lss_inv = self.Kss.cholesky().inverse()
                #end = time.time()
                #print('V method | get Lss_inv : {:.5g}s'.format(end - start))

                V = self.Ksf.T @ Lss_inv.T
                Lambda_inv_sqrt_V = self.Lambda_inv.sqrt() @ V
                Gamma = IdentityLinearOperator(V.shape[1]) + Lambda_inv_sqrt_V.T @ Lambda_inv_sqrt_V

                #start = time.time()
                L_Gamma = torch.linalg.cholesky(Gamma.to_dense())
                #end = time.time()
                #print('V method | Cholesky factorize Γ: {:.5g}s'.format(end - start))
                
                #start = time.time()
                self.U_Sigma = (TriangularLinearOperator(L_Gamma, upper=False).inverse() @ Lss_inv).T
                # the following fails - numerical instability induced by dense representation of Lss_inv
                #aux = TriangularLinearOperator(L_Gamma, upper=False).inverse() @ Lss_inv.to_dense()
                #aux = TriangularLinearOperator(L_Gamma, upper=False).solve_triangular(Lss_inv.to_dense(), upper=False)
                #end = time.time()
                #print('V method | Cholesky inverse: {:.5g}s'.format(end - start))

                #print('V method | cond(L_Γ) = {:.4g}'.format(torch.linalg.cond(L_Gamma).item()))
  
            case 'QR':  # QR method (most stable)
                B = torch.cat([self.Lambda_inv.sqrt() @ self.Ksf.T, Lss.T], dim=0)
                Q, R = stable_qr(B)
                self.U_Sigma = TriangularLinearOperator(R, upper=True).inverse()

        invert_end = time.time()
        #print('{} method | Total inversion time: {:.5g}s'.format(self.invert_mode, invert_end - invert_start))
        #print('Σ symmetry check: Max(|Σ - Σ.T|) = {:.5g} '.format(torch.max(torch.abs((self.Sigma - self.Sigma.T).to_dense()))))

        self.Sigma = self.U_Sigma @ self.U_Sigma.T

        # compute α by doing matrix-vector multiplications first
        self.alpha = self.Lambda_inv @ self.training_outputs
        self.alpha = self.Ksf @ self.alpha
        self.alpha = self.Sigma @ self.alpha
        #self.alpha = self.U_Sigma.T @ self.alpha
        #self.alpha = self.U_Sigma @ self.alpha

    def update_full_set(self, x_train, y_train):
        """
        Update full set without inverting entire updated covariance matrix.
        """
        self.full_descriptors = torch.cat((self.full_descriptors, torch.atleast_2d(x_train)), dim=1)
        self.training_outputs = torch.cat((self.training_outputs, y_train))
        self.Lambda_inv = ConstantDiagLinearOperator(self.model_noise.pow(-2), self.full_descriptors.shape[1])

        Ksfprime = kernel(self.sparse_descriptors, torch.atleast_2d(x_train),
                          self.kernel_noise, self.kernel_length)
        self.Ksf = torch.cat([self.Ksf, Ksfprime], dim=1)

        Lambda_prime = ConstantDiagLinearOperator(self.model_noise.pow(2), torch.atleast_2d(x_train).shape[1])
        to_invert = Lambda_prime + Ksfprime.T @ self.Sigma @ Ksfprime
        # condition numbers are quite low - stabilizd by the noise term
        print('Full set update | cond(to_invert) = {:.4g}'.format(torch.linalg.cond(to_invert.to_dense()).item()))

        start = time.time()
        L_to_invert = torch.linalg.cholesky(to_invert.to_dense())
        end = time.time()
        print('Full set update | Cholesky factorization: {:.5g}s'.format(end - start))

        start = time.time()
        L_inv = TriangularLinearOperator(L_to_invert).inverse()
        end = time.time()
        print('Full set update | Cholesky inversion time: {:.5g}s'.format(end - start))

        aux = L_inv @ Ksfprime.T @ self.Sigma
        self.Sigma = self.Sigma - aux.T @ aux
        self.alpha = self.Sigma @ self.Ksf @ self.Lambda_inv @ self.training_outputs

        self.alpha = self.Lambda_inv @ self.training_outputs
        self.alpha = self.Ksf @ self.alpha
        self.alpha = self.Sigma @ self.alpha
        #self.alpha = self.U_Sigma.T @ self.alpha
        #self.alpha = self.U_Sigma @ self.alpha


    def update_sparse_set(self, x_sparse):
        """
        
        """
        Kssprime = kernel(self.sparse_descriptors, torch.atleast_2d(x_sparse),
                          self.kernel_noise, self.kernel_length)
        self.sparse_descriptors = torch.cat((self.sparse_descriptors, torch.atleast_2d(x_sparse)), dim=1)
        Ksprimesprime = kernel(torch.atleast_2d(x_sparse), torch.atleast_2d(x_sparse),
                               self.kernel_noise, self.kernel_length)
        Kfsprime = kernel(self.full_descriptors, torch.atleast_2d(x_sparse),
                          self.kernel_noise, self.kernel_length)

        # update Kss
        start = time.time()
        Lss_inv_Kssp = self.Kss.cholesky().inverse() @ Kssprime
        Lsp = torch.linalg.cholesky(Ksprimesprime - Lss_inv_Kssp.T @ Lss_inv_Kssp + torch.eye(Ksprimesprime.shape[0]) * 1e-8)
        newLss_upper = torch.cat([self.Kss.cholesky().to_dense(), torch.zeros(Kssprime.shape)], dim=1)
        newLss_lower = torch.cat([Lss_inv_Kssp.T, Lsp.to_dense()], dim=1)
        newLss = torch.cat([newLss_upper, newLss_lower], dim=0)
        self.Kss = CholLinearOperator(newLss, upper=False)
        end = time.time()
        print('Sparse set update | Kss update: {:.5g}s'.format(end - start))
 
        #start = time.time()
        Lss_inv = self.Kss.cholesky().inverse()
        #end = time.time()
        #print('V method | get Lss_inv : {:.5g}s'.format(end - start))

        self.Ksf = torch.cat([self.Ksf, Kfsprime.T], dim=0)

        V = self.Ksf.T @ Lss_inv.T
        Lambda_inv_sqrt_V = self.Lambda_inv.sqrt() @ V
        Gamma = IdentityLinearOperator(V.shape[1]) + Lambda_inv_sqrt_V.T @ Lambda_inv_sqrt_V

        L_Gamma = torch.linalg.cholesky(Gamma.to_dense())

        U_Sigma = (TriangularLinearOperator(L_Gamma, upper=False).inverse() @ Lss_inv).T
        self.Sigma = U_Sigma @ U_Sigma.T

        self.alpha = self.Lambda_inv @ self.training_outputs
        self.alpha = self.Ksf @ self.alpha
        self.alpha = self.Sigma @ self.alpha

        # update Sigma
        #Sigma_prev = self.Sigma
        #B = Kssprime + self.Ksf @ self.Lambda_inv @ Kfsprime
        #D = Ksprimesprime + Kfsprime.T @ self.Lambda_inv @ Kfsprime

        #R_inv_B = self.R_inv.T @ B
        
        #to_invert = D - R_inv_B.T @ R_inv_B
        #evals, evecs = torch.linalg.eigh(to_invert)
        #print(evals)
        
        #aux = self.R_inv.T @ B
        #aux = aux.T @ aux
        #print(torch.max(torch.abs(aux - aux.T)))
        #print(torch.max(torch.abs(B - C.T)))
        #print(torch.max(torch.abs(to_invert - to_invert.T)))
        #print('cond(to_invert) = {:.4g}'.format(torch.linalg.cond(to_invert).item()))
        
        #to_invert = to_invert + torch.eye(to_invert.shape[0]) * 1e-8
        #print('cond(to_invert + jitter) = {:.4g}'.format(torch.linalg.cond(to_invert).item()))
        
        #start = time.time()
        #L = torch.linalg.cholesky(to_invert)
        #L_inv = TriangularLinearOperator(L, upper=False).inverse()
        #newD = L_inv.T @ L_inv
        #newD = torch.linalg.inv(to_invert)
        #end = time.time()
        #print('Cholesky newD: {:.5g}s'.format(end - start))
        
        #newA = Sigma_prev + Sigma_prev @ B @ newD @ B.T @ Sigma_prev
        #newB = -1 * Sigma_prev @ B @ newD
        #newC = -1 * newD @ C @ Sigma_prev

        #newB = -1 * newD @ B.T @ Sigma_prev
        #self.Sigma = newA.cat_rows(newB, newD, generate_roots=False, generate_inv_roots=False)
        
        #self.Ksf = torch.cat([self.Ksf, Kfsprime.T], dim=0)
        #self.alpha = self.Sigma @ self.Ksf @ self.Lambda_inv @ self.training_outputs


    def get_predictions(self, x_test, mean_var=[True, True], mode='dtc'):
        """
        Get predictions of GP model with a set of testing vectors.

        Args:
          x_test (torch.Tensor): d × p tensor where p is the number of x vectors and d is the
                                  dimensionality of the x descriptor vectors
        """
        Kst = kernel(self.sparse_descriptors, x_test,
                     self.kernel_noise, self.kernel_length)
        predictions = []
        if mean_var[0]:
            mean = Kst.T @ self.alpha
            predictions.append(mean)
        if mean_var[1]:
            if mode == 'sor':  # quite nonsensical
                var = torch.abs((Kst.T @ self.Sigma @ Kst).diag())
            elif mode == 'dtc':
                var = kernel(x_test, x_test,
                             self.kernel_noise, self.kernel_length, diag=True)
                Lss_inv_Kst = self.Kss.cholesky(upper=False).solve(Kst)
                var = var - Lss_inv_Kst.pow(2).sum(dim=0)
                #U_Sigma_Kst = self.U_Sigma.T @ Kst
                #var = var + U_Sigma_Kst.pow(2).sum(dim=0)
                var = var + (Kst.T @ self.Sigma @ Kst).diagonal()
            predictions.append(torch.abs(var))
        return predictions

    def get_likelihood(self):
        fsize = self.full_descriptors.shape[1]
        fit_term = -0.5 * self.training_outputs.unsqueeze(0) @ self.Lambda_inv
        fit_term = fit_term @ (self.training_outputs - self.Ksf.T @ self.alpha)
        logdet_Xi_inv = -1 * self.Kss.logdet() - self.Lambda_inv.logdet() - 2 * self.U_Sigma.logdet()
        #logdet_Xi_inv = -1 * self.Kss.logdet() - self.Lambda_inv.logdet() - self.Sigma.logdet()
        return fit_term - 0.5 * logdet_Xi_inv - fsize * 0.5 * np.log(2 * np.pi)

    def optimize_hyperparameters(self, rtol=1e-4, relax_kernel_length=False):
        """
        Optimize hyperparameters
        """
        self.model_noise.requires_grad_()
        self.kernel_noise.requires_grad_()
        self.kernel_length.requires_grad_(relax_kernel_length)
        counter = 0
        self.optimizer.zero_grad()
        self.__update_Sigma_alpha()
        likelihood = self.get_likelihood()
        (-likelihood).backward()
        dlikelihood = np.inf
        prev_likelihood = likelihood.item()
        while np.abs(dlikelihood/prev_likelihood) > rtol:
            counter += 1
            self.optimizer.step()
            self.optimizer.zero_grad()
            self.__update_Sigma_alpha()
            likelihood = self.get_likelihood()
            (-likelihood).backward()
            dlikelihood = np.abs(prev_likelihood - likelihood.item())
            prev_likelihood = likelihood.item()
        self.model_noise.requires_grad_(False)
        self.kernel_noise.requires_grad_(False)
        self.kernel_length.requires_grad_(False)

        self.Kss = self.Kss.detach()
        self.U_Sigma = self.U_Sigma.detach()
        self.Sigma = self.Sigma.detach()
        self.alpha = self.alpha.detach()
        return counter