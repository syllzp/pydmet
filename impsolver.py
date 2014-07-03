#!/usr/bin/env python

import os
import tempfile
import commands
import numpy

from pyscf import lib
from pyscf import ao2mo
import psi4
import junk.molpro_fcidump

FCIEXE = os.path.dirname(__file__) + '/fci'

def cc(mol, nelec, h1e, h2e, ptrace, mo):
    h1e = reduce(numpy.dot, (mo.T, h1e, mo))
    eri = numpy.zeros(h2e.size)
    ij = 0
    for i in range(eri.shape[0]):
        for j in range(i+1):
            eri[ij] = eri[i,j]
            ij += 1
    eri = ao2mo.incore.full(eri1, mo)

    ps = psi4.Solver()
    with psi4.capture_stdout():
        nmo = h1e.shape[0]
        ps.prepare('RHF', numpy.eye(nmo), h1e, h2e, nelec)
        ecc = ps.energy('CCSD')
        rdm1, rdm2 = ps.density()

    nmo = mo.shape[1]
    p = numpy.dot(mo[:ptrace,:].T, mo[:ptrace,:])
    rdm1 = numpy.dot(p, rdm1)
    e1_ptrace = lib.trace_ab(rdm1.reshape(-1), h1e.reshape(-1))
    rdm2 = numpy.dot(p, rdm2.reshape(nmo,-1))
    e2_ptrace = lib.trace_ab(rdm2.reshape(-1), eri.reshape(-1))
    e_ptrace = e1_ptrace + e2_ptrace * .5
    nocc = nelec / 2
    escf = 0
    jk = 0
    for i in range(nocc):
        escf += h1e[i,i]
        ii = i*(i+1)/2 + i
        for j in range(nocc):
            jj = j*(j+1)/2 + j
            if i < j:
                ij = j*(j+1)/2+i
            else:
                ij = i*(i+1)/2+j
            escf += (h2e[ii,jj] - h2e[ij,ij] * .5)
            jk += h2e[ii,jj] - h2e[ij,ij] * .5
    print ecc, escf * 2

    return ecc+escf*2, e_ptrace, reduce(numpy.dot, (mo, rdm1, mo.T))

def fci(mol, nelec, h1e, h2e, ptrace, mo):
    tmpfile = tempfile.mkstemp()[1]
    with open(tmpfile, 'w') as fout:
        junk.molpro_fcidump.head(h1e.shape[0], nelec, fout)
        junk.molpro_fcidump.write_eri_in_molpro_format(h2e, fout)
        junk.molpro_fcidump.write_hcore_in_molpro_format(h1e, fout)
        fout.write(' 0.0  0  0  0  0\n')

    cmd = [FCIEXE,
           '--subspace-dimension=16',
           '--basis=Input',
           '--ptrace=%i' % ptrace]
    filedm1 = tempfile.mkstemp()[1]
    cmd.append('--save-rdm1=%s' % filedm1)
    #if rdm2 is not None:
    #    filedm2 = tempfile.mkstemp()[1]
    #    cmd.append('--save-rdm2=%s' % filedm2)
    cmd.append(tmpfile)

    rec = commands.getoutput(' '.join(cmd))
    e = find_fci_key(rec, '!FCI STATE 1 ENERGY')
    e2_ptrace = find_fci_key(rec, '!FCI STATE 1 pTraceSys')
    res = [e]
    rdm1 = []
    with open(filedm1, 'r') as fin:
        n = int(fin.readline().split()[-1])
        for d in fin.readlines():
            rdm1.append(map(float, d.split()))
    rdm1 = numpy.array(rdm1).reshape(n,n)
    print rdm1.shape
    e1_ptrace = numpy.dot(rdm1[:ptrace].flatten(), h1e[:ptrace].flatten())
    e_ptrace = e1_ptrace + e2_ptrace * .5

    os.remove(filedm1)
    os.remove(tmpfile)
    return e, e_ptrace, rdm1

def find_fci_key(rec, key):
    for dl in rec.splitlines():
        if key in dl:
            val = float(dl.split()[-1])
            break
    return val

def use_local_solver(local_solver, with_rdm1=None):
    def imp_solver(mol, emb, vfit=0):
        h1e = emb.get_hcore(mol)
        if vfit is not 0:
            nv = vfit.shape[0]
            h1e[:nv,:nv] += vfit
        if emb._eri is None:
            eri = emb.eri_on_impbas(mol)
        else:
            eri = emb._eri
        nelec = emb.nelectron
        nimp = len(emb.bas_on_frag)
        res = local_solver(mol, nelec, h1e, eri, nimp, emb.mo_coeff_on_imp)
        return res
    return imp_solver
