#!/usr/bin/env python

import time
import numpy
import scipy.linalg
import copy

from pyscf import scf
import pyscf.lib.parameters as param
import pyscf.lib.logger as log
from pyscf import lo
from pyscf import tools
import pyscf.tools.dump_mat
import dmet_hf
import fitdm
import impsolver
import dmet_sc

class EmbSys(object):
    def __init__(self, mol, entire_scf, frag_group=[], init_v=None,
                 orth_coeff=None):
        self.verbose = mol.verbose
        self.stdout = mol.stdout
        self.mol = mol
        self.OneImp = dmet_hf.RHF

        self.max_iter         = 40
        self.conv_threshold   = 1e-5

        self.orth_coeff = orth_coeff
#        #self.pre_orth_ao = lo.iao.pre_atm_scf_ao(mol)
#        self.pre_orth_ao = numpy.eye(mol.nao_nr())
#        self.orth_ao_method = 'lowdin'
#        self.orth_coeff = lo.orth.orth_ao(mol, 'lowdin', numpy.eye(mol.nao_nr()))

        self.basidx_group = []
        self.entire_scf = entire_scf
        self.embs = []
        self.solver = impsolver.FCI()
        self.vfit_mf = []
        self.vfit_ci = []
        self.leastsq = True
        self.translational = True

        self.vfit_ci_method = gen_all_vfit_by(fit_chemical_potential)


    def build_(self, mol):
        assert(self.orth_coeff is not None)

        embs = []
        self.vfit_mf = []
        self.vfit_ci = []
        for bidx in self.basidx_group:
            emb = self.OneImp(self.entire_scf)
            emb.occ_env_cutoff = 1e-14
            emb.bas_on_frag = bidx
            emb.orth_coeff = self.orth_coeff
            emb.verbose = 0
            embs.append(emb)
            nimp = len(bidx)
            self.vfit_mf.append(numpy.zeros((nimp,nimp)))
            self.vfit_ci.append(numpy.zeros((nimp,nimp)))
            if self.translational:
                break

        self.update_embs(mol, embs, self.entire_scf, self.orth_coeff)
        self.embs = embs

        nao = self.orth_coeff.shape[1]
        return self.vfit_mf, self.vfit_ci

    # update the embs in terms of the given entire_scf
    def update_embs(self, mol, embs, eff_scf, orth_coeff):
        sc = numpy.dot(eff_scf.get_ovlp(), eff_scf.mo_coeff)
        c_inv = numpy.dot(eff_scf.get_ovlp(), orth_coeff).T
        fock0 = numpy.dot(sc*eff_scf.mo_energy, sc.T.conj())
        hcore = eff_scf.get_hcore()

        nocc = int(eff_scf.mo_occ.sum()) / 2

        for ifrag, emb in enumerate(embs):
            mo_orth = numpy.dot(c_inv, eff_scf.mo_coeff[:,eff_scf.mo_occ>1e-15])
            emb.imp_site, emb.bath_orb, emb.env_orb = \
                    dmet_hf.decompose_orbital(emb, mo_orth, emb.bas_on_frag)
            emb.impbas_coeff = emb.cons_impurity_basis()
            emb.nelectron = mol.nelectron - emb.env_orb.shape[1] * 2
            log.debug(emb, 'nelec of emb %d = %d', ifrag, emb.nelectron)
            emb._eri = emb.eri_on_impbas(mol)
            emb.energy_by_env, emb._vhf_env = emb.init_vhf_env(emb.env_orb)

            emb._project_fock = emb.mat_ao2impbas(fock0)
            emb.mo_energy, emb.mo_coeff_on_imp = scipy.linalg.eigh(emb._project_fock)
            emb.mo_coeff = numpy.dot(emb.impbas_coeff, emb.mo_coeff_on_imp)
            emb.mo_occ = numpy.zeros_like(emb.mo_energy)
            emb.mo_occ[:emb.nelectron/2] = 2
            emb.hf_energy = 0
            nimp = emb.imp_site.shape[1]
            cimp = numpy.dot(emb.impbas_coeff[:,:nimp].T, sc[:,:nocc])
            emb._pure_hcore = emb.mat_ao2impbas(hcore)
            emb._project_nelec_frag = numpy.linalg.norm(cimp)**2*2
            log.debug(self, 'project_nelec_frag = %f', emb._project_nelec_frag)

#            if isinstance(self.vfit_mf, numpy.ndarray):
#                v1 = emb.mat_orthao2impbas(self.vfit_mf)
#                v1[:nimp,:nimp] = 0
#                emb._vhf_env += v1
        return embs


    def mat_orthao2ao(self, mat):
        '''matrix represented on orthogonal basis to the representation on
        non-orth AOs'''
        c_inv = numpy.dot(self.orth_coeff.T, self.entire_scf.get_ovlp())
        mat_on_ao = reduce(numpy.dot, (c_inv.T, mat, c_inv))
        return mat_on_ao

    def run_hf_with_ext_pot_(self, vext_on_ao):
        eff_scf = self.entire_scf
        h = eff_scf.get_hcore()
        eff_scf.get_hcore = lambda *args: h + vext_on_ao

        dm0 = eff_scf.make_rdm1(eff_scf.mo_coeff, eff_scf.mo_occ)

        eff_scf.scf_conv, eff_scf.hf_energy, eff_scf.mo_energy, \
                eff_scf.mo_coeff, eff_scf.mo_occ \
                = scf.hf.kernel(eff_scf, eff_scf.conv_tol, dump_chk=False,
                                dm0=dm0)
        # must release the modified get_hcore to get pure hcore
        del(eff_scf.get_hcore)
        return eff_scf

    def update_embsys(self, mol, vfit_mf, vfit_ci):
        v_add = self.assemble_to_blockmat(vfit_mf)
        v_add_ao = self.mat_orthao2ao(v_add)
        self.entire_scf = self.run_hf_with_ext_pot_(v_add_ao)
        for emb in self.embs:
            emb.entire_scf = self.entire_scf

        self.update_embs(mol, self.embs, self.entire_scf, self.orth_coeff)
        self.vfit_mf = vfit_mf
        self.vfit_ci = vfit_ci


    def assemble_frag_energy(self, mol):
        e_tot = 0
        nelec = 0
        for m, emb in enumerate(self.embs):
            nimp = len(emb.bas_on_frag)
            dv = numpy.eye(nimp) * self.vfit_ci[m]
            _, e2frag, dm1 = \
                    self.solver.run(emb, emb._eri, dv,
                                    with_1pdm=True, with_e2frag=nimp)
            e_frag, nelec_frag = \
                    self.extract_frag_energy(emb, dm1, e2frag)

            log.debug(self, 'fragment %d FCI-in-HF, frag energy = %.12g, nelec = %.9g', \
                      m, e_frag, nelec_frag)
            e_tot += e_frag
            nelec += nelec_frag
        if self.translational:
            nao = self.orth_coeff.shape[0]
            nimp = len(self.embs[0].bas_on_frag)
            e_tot = e_tot * (nao//nimp)
            nelec = nelec * (nao//nimp)
        log.info(self, 'sum(e_frag), energy = %.9g, nelec = %.9g',
                  e_tot, nelec)
        return e_tot, 0, nelec

    def extract_frag_energy(self, emb, dm1, e2frag):
        nimp = len(emb.bas_on_frag)

        if emb._pure_hcore is not None:
            h1e = emb._pure_hcore
        else:
            h1e = emb.mat_ao2impbas(emb.entire_scf.get_hcore(emb.mol))

        e1_frag = numpy.dot(dm1[:nimp,:nimp].flatten(),h1e[:nimp,:nimp].flatten())
        e1_bath = numpy.dot(dm1[:nimp,nimp:].flatten(),h1e[:nimp,nimp:].flatten())
#        if self.env_pot_for_ci and emb.vfit_ci is not 0:
#            e1_vfit = numpy.dot(dm1[:nimp].flatten(), emb.vfit_ci[:nimp].flatten())
#        else:
#            e1_vfit = 0
        e1_vfit = 0
        e1 = e1_frag + e1_bath + e1_vfit
        log.debug(emb, 'e1 = %.12g = fragment + bath + fitenv = %.12g + %.12g + %.12g', \
                  e1, e1_frag, e1_bath, e1_vfit)

        e2env_hf = numpy.dot(dm1[:nimp].flatten(), \
                             emb._vhf_env[:nimp].flatten()) * .5
        nelec_frag = dm1[:nimp].trace()
        e_frag = e1 + e2env_hf + e2frag
        log.debug(emb, 'fragment e1 = %.12g, e2env_hf = %.12g, FCI pTraceSys = %.12g, sum = %.12g', \
                  e1, e2env_hf, e2frag, e_frag)
        log.debug(emb, 'fragment e2env_hf = %.12g, FCI pTraceSys = %.12g, nelec = %.12g', \
                  e2env_hf, e2frag, nelec_frag)

        return e_frag, nelec_frag


    def unpack(self, v_group):
        return self.assemble_to_blockmat(v_group)
    def assemble_to_blockmat(self, v_group):
        '''assemble matrix on impuity sites to the diagonal block'''
        nao = self.orth_coeff.shape[1]
        v_add = numpy.zeros((nao,nao))
        if self.translational:
            nimp = len(self.basidx_group[0])
            for m,basidx in enumerate(self.basidx_group):
                bidx = numpy.array(basidx)
                v_add[bidx[:,None],bidx] = v_group[0][:nimp,:nimp]
        else:
            for m, basidx in enumerate(self.basidx_group):
                nimp = len(basidx)
                bidx = numpy.array(basidx)
                v_add[bidx[:,None],bidx] = v_group[m][:nimp,:nimp]
        return v_add

    def pack(self, v):
        v_group = []
        for i,basidx in enumerate(self.basidx_group):
            bidx = numpy.array(basidx)
            v_group.append(v[bidx[:,None],bidx])
        return v_group

    def dump_frag_prop_mat(self, mol, frag_mat_group):
        '''dump fragment potential or density matrix'''
        for m, v in enumerate(frag_mat_group):
            try:
                pyscf.tools.dump_mat.dump_tri(self.stdout, v)
            except:
                self.stdout.write('%s\n' % str(v))

    def fit_solver(self, fock0, nocc, dm_ref_alpha):
        nao = fock0.shape[0]
        mol = self.mol
        c_inv = numpy.dot(self.entire_scf.get_ovlp(), self.orth_coeff).T
        assert(nocc == mol.nelectron // 2)
        if self.translational:
            assert(len(self.embs) == 1)
        def _decompress(vfit):
            p0 = 0
            v = numpy.zeros_like(fock0)
            for m,emb in enumerate(self.embs):
                nimp = len(emb.bas_on_frag)
                bidx = numpy.array(emb.bas_on_frag)
                idx = numpy.tril_indices(nimp)
                v1 = numpy.empty((nimp,nimp))
                p1 = p0 + nimp*(nimp+1)//2
                v1[idx] = vfit[p0:p1]
                v1[idx[1],idx[0]] = vfit[p0:p1]
                v[bidx[:,None],bidx] = v1
                p0 = p1
            if self.translational:
                bidx = numpy.array(self.basidx_group[0])
                v = self.unpack([v[bidx[:,None],bidx]])
            return v

        def diff_dm(vfit):
            f = fock0+_decompress(vfit)
            e, c = scipy.linalg.eigh(f)
            ddm = []
            for m,emb in enumerate(self.embs):
                bidx = numpy.array(emb.bas_on_frag)
                nimp = len(bidx)
                if self.dm_fit_domain == dmet_sc.IMP_BLK:
                    c1 = c[bidx]
                    dm_ref = dm_ref_alpha[m][:nimp,:nimp]
                else:
                    c1 = numpy.vstack((c[bidx], numpy.dot(emb.bath_orb.T,c)))
                    dm_ref = dm_ref_alpha[m]
                dm0 = numpy.dot(c1[:,:nocc], c1[:,:nocc].T)
                ddm.append((dm0-dm_ref).ravel())
            return numpy.hstack(ddm)

        def jac_ddm(vfit, *args):
            e, c = scipy.linalg.eigh(fock0+_decompress(vfit))
            nao, nmo = c.shape
            nvir = nmo - nocc
            eia = 1 / (e[:nocc].reshape(nocc,1) - e[nocc:])
            ddm = []
            xtmp = []
            for m,emb in enumerate(self.embs):
                bidx = numpy.array(emb.bas_on_frag)
                nimp = len(bidx)
                if self.dm_fit_domain == dmet_sc.IMP_BLK:
                    c1 = c[bidx]
                    dm_ref = dm_ref_alpha[m][:nimp,:nimp]
                else:
                    c1 = numpy.vstack((c[bidx], numpy.dot(emb.bath_orb.T,c)))
                    dm_ref = dm_ref_alpha[m]
                dm0 = numpy.dot(c1[:,:nocc], c1[:,:nocc].T)
                ddm.append((dm0-dm_ref).ravel())
                nf = c1.shape[0]

                tmpcc = numpy.einsum('ik,jk->kij', c1, c)
                v = tmpcc.reshape(nmo,-1)
                _x = reduce(numpy.dot, (v[nocc:].T, eia.T, v[:nocc]))
                _x = _x.reshape(nf,nao,nf,nao) # nf for dm, nao for v
                x0 = _x.transpose(0,2,1,3)
                x1 = x0.transpose(1,0,3,2)
                xx = x0 + x1
                xtmp.append(xx.reshape(nf*nf,-1))
            xtmp = numpy.vstack(xtmp).reshape(-1,nao,nao)
            x = []
            for m,basidx in enumerate(self.basidx_group):
                bidx = numpy.array(basidx)
                nimp = len(bidx)
                tmp = xtmp[:,bidx[:,None],bidx]
                usymm = symm_trans_mat_for_hermit(nimp)
                nn = usymm.shape[0]
                x.append(numpy.dot(tmp.reshape(-1,nn), usymm))
            x = numpy.hstack(x)
            if self.translational:
                nimp = len(self.basidx_group[0])
                n2 = nimp * (nimp+1) // 2
                x0 = numpy.zeros((x.shape[0],n2))
                for m,basidx in enumerate(self.basidx_group):
                    x0 += x[:,m*n2:(m+1)*n2]
                x = x0
            return x

        p0 = 0
        for m,emb in enumerate(self.embs):
            nimp = len(emb.bas_on_frag)
            p0 += nimp * (nimp+1) // 2
        vfit0 = numpy.zeros(p0)
        if self.leastsq:
            x = scipy.optimize.leastsq(diff_dm, vfit0, Dfun=jac_ddm,
                                       ftol=1e-8, maxfev=40)
            log.debug(self, 'norm(ddm) %s', numpy.linalg.norm(diff_dm(x[0])))
            sol = _decompress(x[0])
        else:
            x = scipy.optimize.minimize(lambda x:numpy.linalg.norm(diff_dm(x))**2,
                                        vfit0,
                                        jac=lambda x:numpy.einsum('i,ij->j',diff_dm(x),jac_ddm(x)),
                                        options={'disp':False}).x
            log.debug(self, 'norm(ddm) %s', numpy.linalg.norm(diff_dm(x)))
            sol = _decompress(x)

        sol -= numpy.eye(nao)*sol.diagonal().mean()
        vfit_mf = []
        for m, emb in enumerate(self.embs):
            bidx = numpy.array(emb.bas_on_frag)
            vfit_mf.append(sol[bidx[:,None],bidx])
        return vfit_mf


    def scdmet(self):
        embsys = self
        mol = embsys.mol

        vfit_mf = embsys.build_(mol)[0]
        vfit_ci = embsys.vfit_ci_method(mol, embsys)
        embsys.vfit_ci = vfit_ci
        vfit_ci = embsys.vfit_ci
        # to guarantee correct number of electrons, calculate embedded energy
        # before calling update_embsys
        e_tot, e_corr, nelec = embsys.assemble_frag_energy(mol)
        log.info(embsys, 'macro iter = 0, e_tot = %.12g, nelec = %g', e_tot, nelec)
        vfit_mf_old = embsys.unpack(vfit_mf)
        vfit_ci_old = vfit_ci
        e_tot_old = e_tot
        e_corr_old = e_corr

        for icyc in range(embsys.max_iter):

            #log.debug(embsys, '  HF energy = %.12g', embsys.entire_scf.hf_energy)
            vfit_mf = embsys.vfit_mf_method(mol, embsys)
            embsys.update_embsys(mol, vfit_mf, vfit_ci)

            vfit_ci = embsys.vfit_ci_method(mol, embsys)
            embsys.vfit_ci = vfit_ci

            # to guarantee correct number of electrons, calculate embedded energy
            # before calling update_embsys
            e_tot, e_corr, nelec = embsys.assemble_frag_energy(mol)

            dv = embsys.unpack(vfit_mf) - vfit_mf_old
            dv[numpy.diag_indices(dv.shape[0])] = 0
            dv = numpy.linalg.norm(dv)

            vfit_mf_old = embsys.unpack(vfit_mf)
            vfit_ci_old = vfit_ci
            e_tot_old = e_tot
            e_corr_old = e_corr

            log.info(embsys, 'macro iter = %d, e_tot = %.12g, e_tot(corr) = %.12g, nelec = %g, dv = %g', \
                     icyc+1, e_tot, e_corr, nelec, dv)
            de = abs(1-e_tot_old/e_tot)
            decorr = abs(e_corr-e_corr_old)
            log.info(embsys, '                 delta_e = %.12g, (~ %g%%), delta_e(corr) = %.12g', \
                     e_tot-e_tot_old, de * 100, decorr)

            log.debug(embsys, 'CPU time %.8g' % time.clock())

            if dv < 1e-5:
                break

        return e_tot, vfit_mf, vfit_ci

    def vfit_mf_method(self, mol, embsys):
        dm_ref = []
        for m,emb in enumerate(self.embs):
            nimp = len(emb.bas_on_frag)
            dv = numpy.eye(nimp) * embsys.vfit_ci[0]
            dmci = embsys.solver.run(emb, emb._eri, dv, True, False)[2]
            log.debug(embsys, 'dm_ref %d = %s', m, dmci)
            dm_ref.append(dmci*.5)
        if self.translational:
            dm_ref = [dm_ref[0] for i in self.basidx_group]

        sc = reduce(numpy.dot, (self.orth_coeff.T,
                                self.entire_scf.get_ovlp(),
                                self.entire_scf.mo_coeff))
        # this fock matrix includes the previous fitting potential
        fock0 = numpy.dot(sc*self.entire_scf.mo_energy, sc.T.conj())
        nocc = mol.nelectron // 2

        dv = embsys.fit_solver(fock0, nocc, dm_ref)
        v = [embsys.vfit_mf[m]+dv[m] for m,emb in enumerate(self.embs)]
        log.debug(self, 'vfit_mf = ')
        try:
            v_group = self.pack(v)
            for vi in v_group:
                pyscf.tools.dump_mat.dump_tri(self.stdout, vi)
        except:
            self.stdout.write('%s\n' % str(v))
        return v


    def dump_frag_prop_mat(self, mol, frag_mat_group):
        '''dump fragment potential or density matrix'''
        for m, v in enumerate(frag_mat_group):
            try:
                pyscf.tools.dump_mat.dump_tri(self.stdout, v)
            except:
                self.stdout.write('%s\n' % str(v))
###########################################################
# fitting methods
###########################################################

def gen_all_vfit_by(local_fit_method):
    '''fit HF DM with chemical potential'''
    def fitloop(mol, embsys):
        v_group = []
        for m,emb in enumerate(embsys.embs):
            dv = local_fit_method(mol, m, embsys)
            v_group.append(dv)

        if embsys.verbose >= param.VERBOSE_DEBUG:
            log.debug(embsys, 'fitting potential =')
            embsys.dump_frag_prop_mat(mol, v_group)
        return v_group
    return fitloop


def fit_chemical_potential(mol, m, embsys):
# correlation potential of embedded-HF is not added to correlated-solver
    emb = embsys.embs[m]
    import scipy.optimize
    nimp = len(emb.bas_on_frag)
    nelec_frag = emb._project_nelec_frag

# change chemical potential to get correct number of electrons
    def nelec_diff(v):
        dv = numpy.eye(nimp) * v
        dm = embsys.solver.run(emb, emb._eri, dv, True, False)[2]
        #print 'ddm ',nelec_frag,dm[:nimp].trace(), nelec_frag - dm[:nimp].trace()
        return nelec_frag - dm[:nimp].trace()
    sol = scipy.optimize.root(nelec_diff, 0, tol=1e-3, \
                              method='lm', options={'ftol':1e-3, 'maxiter':12})
    log.debug(embsys, 'scipy.optimize summary %s', sol)
    log.debug(embsys, 'chem potential = %.11g, nelec error = %.11g', \
              sol.x, sol.fun)
    log.debug(embsys, '        ncall = %d, scipy.optimize success: %s', \
              sol.nfev, sol.success)
    return sol.x[0]


def symm_trans_mat_for_hermit(n):
    # transformation matrix to remove the antisymmetric mode
    # usym is the symmetrized vector corresponding to symmetric component.
    usym = numpy.zeros((n*n, n*(n+1)/2))
    for i in range(n):
        for j in range(i):
            usym[i*n+j,i*(i+1)/2+j] = 1
            usym[j*n+i,i*(i+1)/2+j] = 1
        usym[i*n+i,i*(i+1)/2+i] = 1
    return usym





if __name__ == '__main__':
    from pyscf import gto
    from pyscf import scf

    mol = gto.Mole()
    mol.verbose = 5

    b1 = 1.1
    nat = 10
    mol.atom = []
    r = b1/2 / numpy.sin(numpy.pi/nat)
    for i in range(nat):
        theta = i * (2*numpy.pi/nat)
        mol.atom.append((1, (r*numpy.cos(theta),
                             r*numpy.sin(theta), 0)))

    mol.basis = {'H': 'sto3g',}
    mol.build(False, False)
    mf = scf.RHF(mol)
    mf.verbose = 0
    print mf.scf()

    embsys = EmbSys(mol, mf)
    embsys.basidx = [0,1]
    embsys.max_iter = 10
    embsys.scdmet() # -18.0179909364


