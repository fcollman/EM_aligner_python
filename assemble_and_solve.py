#!/usr/bin/env python

from pymongo import MongoClient
import numpy as np
import renderapi
from EM_aligner_python_schema import *
import copy
import time
import scipy.sparse as sparse
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import spsolve, factorized
import h5py
import os
import sys

def make_dbconnection(collection,which='tile'):
    #connect to the database
    if collection['db_interface']=='mongo':
        client = MongoClient(host=collection['mongo_host'],port=collection['mongo_port'])
        if collection['collection_type']=='stack':
            #for getting shared transforms, which='transform'
            mongo_collection_name = collection['owner']+'__'+collection['project']+'__'+collection['name']+'__'+which
            dbconnection = client.render[mongo_collection_name]
        elif collection['collection_type']=='pointmatch':
            mongo_collection_name = collection['owner']+'__'+collection['name']
            dbconnection = client.match[mongo_collection_name]
    elif collection['db_interface']=='render':
        dbconnection = renderapi.connect(**collection)
    else:
        print 'invalid interface in make_dbconnection()'
        return
    return dbconnection

def get_tileids_and_tforms(stack,tform_obj,zvals):
    #connect to the database
    dbconnection = make_dbconnection(stack)

    tile_ids = []
    tile_tforms = []
    tile_tspecs = []
    shared_tforms = []
    t0 = time.time()

    for z in zvals:
        #load tile specs from the database
        if stack['db_interface']=='render':
            tmp = renderapi.resolvedtiles.get_resolved_tiles_from_z(stack['name'],float(z),render=dbconnection,owner=stack['owner'],project=stack['project'])
            tspecs = tmp.tilespecs
            for st in tmp.transforms:
                shared_tforms.append(st)
        if stack['db_interface']=='mongo':
            #cursor = dbconnection.find({'z':float(z)}) #no order
            cursor = dbconnection.find({'z':float(z)}).sort([('layout.imageRow',1),('layout.imageCol',1)]) #like renderapi order?
            tspecs = list(cursor)
            refids = []
            for ts in tspecs:
                for m in np.arange(len(ts['transforms']['specList'])):
                    if 'refId' in ts['transforms']['specList'][m]:
                        refids.append(ts['transforms']['specList'][m]['refId'])
            refids = np.unique(np.array(refids))

            #be selective of which transforms to pass on to the new stack
            dbconnection2 = make_dbconnection(stack,which='transform')
            for refid in refids:
                shared_tforms.append(renderapi.transform.load_transform_json(list(dbconnection2.find({"id":refid}))[0]))

        #make lists of IDs and transforms
        for k in np.arange(len(tspecs)):
            if stack['db_interface']=='render':
                tile_ids.append(tspecs[k].tileId)
                if 'affine' in tform_obj.name:
                    tile_tforms.append([tspecs[k].tforms[-1].M[0,0],tspecs[k].tforms[-1].M[0,1],tspecs[k].tforms[-1].M[0,2],tspecs[k].tforms[-1].M[1,0],tspecs[k].tforms[-1].M[1,1],tspecs[k].tforms[-1].M[1,2]])
                elif tform_obj.name=='rigid':
                    tile_tforms.append([tspecs[k].tforms[-1].M[0,0],tspecs[k].tforms[-1].M[0,1],tspecs[k].tforms[-1].M[0,2],tforms[-1].M[1,2]])
            if stack['db_interface']=='mongo':
                tile_ids.append(tspecs[k]['tileId'])
                if 'affine' in tform_obj.name:
                    tile_tforms.append(np.array(tspecs[k]['transforms']['specList'][-1]['dataString'].split()).astype('float')[[0,2,4,1,3,5]])
                elif tform_obj.name=='rigid':
                    tile_tforms.append(np.array(tspecs[k]['transforms']['specList'][-1]['dataString'].split()).astype('float')[[0,2,4,5]])
                tspecs[k] = renderapi.tilespec.TileSpec(json=tspecs[k]) #move to renderapi object
            tile_tspecs.append(tspecs[k])

    print 'loaded %d tile specs from %d zvalues in %0.1f sec using interface: %s'%(len(tile_ids),len(zvals),time.time()-t0,stack['db_interface'])
    return np.array(tile_ids),np.array(tile_tforms).flatten(),np.array(tile_tspecs).flatten(),shared_tforms

class transform_csr:
    #class to convert a tile pair pointmatch dict into a CSR
    def __init__(self,name,nmin,nmax):
        self.nmin = nmin
        self.nmax = nmax
        self.name=name
        if self.name=='affine':
            self.DOF_per_tile=6
            self.nnz_per_row=6
            self.rows_per_ptmatch=1
        if self.name=='affine_fullsize':
            self.DOF_per_tile=6
            self.nnz_per_row=6
            self.rows_per_ptmatch=2
        if self.name=='rigid':
            self.DOF_per_tile=4
            self.nnz_per_row=6
            self.rows_per_ptmatch=4

        #allocate some space
        self.data = np.zeros(nmax*self.nnz_per_row*self.rows_per_ptmatch).astype('float64')
        self.indices = np.zeros(nmax*self.nnz_per_row*self.rows_per_ptmatch).astype('int64')
        self.indptr = np.zeros(nmax*self.rows_per_ptmatch).astype('int64')
        self.weights = np.zeros(nmax*self.rows_per_ptmatch).astype('float64')

    def CSR_tile_pair(self,match,tile_ind1,tile_ind2):
       npts = len(match['matches']['q'][0])
       if npts > self.nmax: #really dumb filter for limiting size
           npts=self.nmax
       if npts<self.nmin:
           return None,None,None,None
       self.npts=npts

       m = np.arange(npts)
       mstep = m*self.nnz_per_row
       if self.name=='affine_fullsize':
           #u=ax+by+c
           self.data[0+mstep] = np.array(match['matches']['p'][0])[m]
           self.data[1+mstep] = np.array(match['matches']['p'][1])[m]
           self.data[2+mstep] = 1.0
           self.data[3+mstep] = -1.0*np.array(match['matches']['q'][0])[m]
           self.data[4+mstep] = -1.0*np.array(match['matches']['q'][1])[m]
           self.data[5+mstep] = -1.0
           uindices = np.hstack((tile_ind1*self.DOF_per_tile+np.array([0,1,2]),tile_ind2*self.DOF_per_tile+np.array([0,1,2])))
           self.indices[0:npts*self.nnz_per_row] = np.tile(uindices,npts) 
           #v=dx+ey+f
           self.data[(npts*self.nnz_per_row):(2*npts*self.nnz_per_row)] = self.data[0:npts*self.nnz_per_row]
           self.indices[npts*self.nnz_per_row:2*npts*self.nnz_per_row] = np.tile(uindices+3,npts) 
           self.indptr[0:2*npts] = np.arange(1,2*npts+1)*self.nnz_per_row
           self.weights[0:2*npts] = np.tile(np.array(match['matches']['w'])[m],2)
       if self.name=='affine':
           #u=ax+by+c
           self.data[0+mstep] = np.array(match['matches']['p'][0])[m]
           self.data[1+mstep] = np.array(match['matches']['p'][1])[m]
           self.data[2+mstep] = 1.0
           self.data[3+mstep] = -1.0*np.array(match['matches']['q'][0])[m]
           self.data[4+mstep] = -1.0*np.array(match['matches']['q'][1])[m]
           self.data[5+mstep] = -1.0
           uindices = np.hstack((tile_ind1*self.DOF_per_tile/2+np.array([0,1,2]),tile_ind2*self.DOF_per_tile/2+np.array([0,1,2])))
           self.indices[0:npts*self.nnz_per_row] = np.tile(uindices,npts) 
           self.indptr[0:npts] = np.arange(1,npts+1)*self.nnz_per_row
           self.weights[0:npts] = np.array(match['matches']['w'])[m]
           #don't do anything for v

       if self.name=='rigid':
           px = np.array(match['matches']['p'][0])[m]
           py = np.array(match['matches']['p'][1])[m]
           qx = np.array(match['matches']['q'][0])[m]
           qy = np.array(match['matches']['q'][1])[m]
           #u=ax+by+c
           self.data[0+mstep] = px
           self.data[1+mstep] = py
           self.data[2+mstep] = 1.0
           self.data[3+mstep] = -1.0*qx
           self.data[4+mstep] = -1.0*qy
           self.data[5+mstep] = -1.0
           uindices = np.hstack((tile_ind1*self.DOF_per_tile+np.array([0,1,2]),tile_ind2*self.DOF_per_tile+np.array([0,1,2])))
           self.indices[0:npts*self.nnz_per_row] = np.tile(uindices,npts) 
           #v=-bx+ay+d
           self.data[0+mstep+npts*self.nnz_per_row] = -1.0*px
           self.data[1+mstep+npts*self.nnz_per_row] = py
           self.data[2+mstep+npts*self.nnz_per_row] = 1.0
           self.data[3+mstep+npts*self.nnz_per_row] = 1.0*qx
           self.data[4+mstep+npts*self.nnz_per_row] = -1.0*qy
           self.data[5+mstep+npts*self.nnz_per_row] = -1.0
           vindices = np.hstack((tile_ind1*self.DOF_per_tile+np.array([1,0,3]),tile_ind2*self.DOF_per_tile+np.array([1,0,3])))
           self.indices[npts*self.nnz_per_row:2*npts*self.nnz_per_row] = np.tile(vindices,npts) 
           #du
           self.data[0+mstep+2*npts*self.nnz_per_row] = px-px.mean()
           self.data[1+mstep+2*npts*self.nnz_per_row] = py-py.mean()
           self.data[2+mstep+2*npts*self.nnz_per_row] = 0.0
           self.data[3+mstep+2*npts*self.nnz_per_row] = -1.0*(qx-qx.mean())
           self.data[4+mstep+2*npts*self.nnz_per_row] = -1.0*(qy-qy.mean())
           self.data[5+mstep+2*npts*self.nnz_per_row] = -0.0
           self.indices[2*npts*self.nnz_per_row:3*npts*self.nnz_per_row] = np.tile(uindices,npts) 
           #dv
           self.data[0+mstep+3*npts*self.nnz_per_row] = -1.0*(px-px.mean())
           self.data[1+mstep+3*npts*self.nnz_per_row] = py-py.mean()
           self.data[2+mstep+3*npts*self.nnz_per_row] = 0.0
           self.data[3+mstep+3*npts*self.nnz_per_row] = 1.0*(qx-qx.mean())
           self.data[4+mstep+3*npts*self.nnz_per_row] = -1.0*(qy-qy.mean())
           self.data[5+mstep+3*npts*self.nnz_per_row] = -0.0
           self.indices[3*npts*self.nnz_per_row:4*npts*self.nnz_per_row] = np.tile(uindices,npts) 
  
           self.indptr[0:self.rows_per_ptmatch*npts] = np.arange(1,self.rows_per_ptmatch*npts+1)*self.nnz_per_row
           self.weights[0:self.rows_per_ptmatch*npts] = np.tile(np.array(match['matches']['w'])[m],self.rows_per_ptmatch)

       return self.data[0:npts*self.rows_per_ptmatch*self.nnz_per_row],self.indices[0:npts*self.rows_per_ptmatch*self.nnz_per_row],self.indptr[0:npts*self.rows_per_ptmatch],self.weights[0:npts*self.rows_per_ptmatch]

def write_chunk_to_file(fname,file_number,c,indptr_offset,file_weights,vec_offsets):
    ### data file
    print ' writing to file: %s'%fname
    fcsr = h5py.File(fname,"w")
    #indptr
    #if file_number==0:
    indptr_dset = fcsr.create_dataset("indptr",(c.indptr.size,1),dtype='int64')
    indptr_dset[:] = (c.indptr+indptr_offset).reshape(c.indptr.size,1)
    #else:
        #because all the files concatenated will be a single valid csr matrix
    #    indptr_dset = fcsr.create_dataset("indptr",(c.indptr[1:].size,1),dtype='int64')
    #    indptr_dset[:] = (c.indptr[1:]+indptr_offset).reshape(c.indptr[1:].size,1)
    #indices
    indices_dset = fcsr.create_dataset("indices",(c.indices.size,1),dtype='int64')
    indices_dset[:] = c.indices.reshape(c.indices.size,1)
    #data
    data_dset = fcsr.create_dataset("data",(c.data.size,),dtype='float64')
    data_dset[:] = c.data
    #weights
    weights_dset = fcsr.create_dataset("weights",(file_weights.size,),dtype='float64')
    weights_dset[:] = file_weights
    print ' wrote %s\n %0.2fGB on disk'%(fname,os.path.getsize(fname)/(2.**30))

    ###index.txt
    fmode='a'
    if file_number==0:
        fmode='w'
    tmp = fname.split('/')
    fdir = ''
    for t in tmp[:-1]:
        fdir=fdir+'/'+t
    f=open(fdir+'/index.txt',fmode)
    imesg =  'file %s '%tmp[-1]
    imesg += 'nrow %ld mincol %ld maxcol %ld nnz %ld\n'%(indptr_dset.size-1,c.indices.min(),c.indices.max(),c.indices.size)
    #imesg += 'inp off %ld n %ld '%(vec_offsets[0],indptr_dset.size)
    #imesg += 'ind off %ld n %ld '%(vec_offsets[1],indices_dset.size)
    #imesg += 'dat off %ld n %ld '%(vec_offsets[2],data_dset.size)
    #imesg += 'wts off %ld n %ld\n'%(vec_offsets[3],weights_dset.size)
    f.write(imesg)
    f.close()
 
    #track global offsets
    vec_offsets[0]+=indptr_dset.size
    vec_offsets[1]+=indices_dset.size
    vec_offsets[2]+=data_dset.size
    vec_offsets[3]+=weights_dset.size
    fcsr.close()

    return vec_offsets

def get_matches(zi,zj,collection,dbconnection):
    if collection['db_interface']=='render':
        if zi==zj:
            matches = renderapi.pointmatch.get_matches_within_group(collection['name'],str(float(zi)),owner=collection['owner'],render=dbconnection)
        else:
            matches = renderapi.pointmatch.get_matches_from_group_to_group(collection['name'],str(float(zi)),str(float(zj)),owner=collection['owner'],render=dbconnection)
        matches = np.array(matches)
    if collection['db_interface']=='mongo':
        cursor = dbconnection.find({'pGroupId':str(float(zi)),'qGroupId':str(float(zj))})
        matches = np.array(list(cursor))
        if zi!=zj:
            #in principle, this does nothing if zi < zj, but, just in case
            cursor = dbconnection.find({'pGroupId':str(float(zj)),'qGroupId':str(float(zi))})
            matches = np.append(matches,list(cursor))
    return matches

def tilepair_weight(i,j,matrix_assembly):
    if i==j:
        tp_weight = matrix_assembly['montage_pt_weight']
    else:
        tp_weight = matrix_assembly['cross_pt_weight']
        if matrix_assembly['inverse_dz']:
            tp_weight = tp_weight/np.abs(j-i+1)
    return tp_weight

def create_CSR_A(collection,matrix_assembly,tform_obj,tile_ids,zvals,output_options):
    #connect to the database
    dbconnection = make_dbconnection(collection)

    file_number=0

    sorter = np.argsort(tile_ids)
    file_chunks = 0
    tiles_used = []

    if output_options['chunks_per_file']==-1:
        nmod=0.1 # np.mod(n+1,0.1) never == 0
    else:
        nmod = output_options['chunks_per_file']

    file_zlist=[]

    for i in np.arange(len(zvals)):
        print ' upward-looking for z %d'%zvals[i]
        jmax = np.min([i+matrix_assembly['depth']+1,len(zvals)])
        for j in np.arange(i,jmax): #depth, upward looking
            t0=time.time()

            matches = get_matches(zvals[i],zvals[j],collection,dbconnection)

            if len(matches)==0:
                print 'WARNING: %d matches for z1=%d z2=%d in pointmatch collection'%(len(matches),zvals[i],zvals[j])
                continue

            #extract IDs for fast checking
            pids = []
            qids = []
            for m in matches:
                pids.append(m['pId'])
                qids.append(m['qId'])
            pids = np.array(pids)
            qids = np.array(qids)

            #remove matches that don't have both IDs in tile_ids
            instack = np.in1d(pids,tile_ids)&np.in1d(qids,tile_ids)
            matches = matches[instack]
            pids = pids[instack]
            qids = qids[instack]
 
            if len(matches)==0:
                print 'WARNING: no tile pairs in stack for pointmatches in z1=%d z2=%d'%(zvals[i],zvals[j])
                continue

            print '  loaded %d matches, using %d, for z1=%d z2=%d in %0.1f sec using interface: %s'%(instack.size,len(matches),zvals[i],zvals[j],time.time()-t0,collection['db_interface'])
        
            t0 = time.time()
            
            #for the given point matches, these are the indices in tile_ids 
            #these determine the column locations in A for each tile pair
            #this is a fast version of np.argwhere() loop
            pinds = sorter[np.searchsorted(tile_ids,pids,sorter=sorter)]
            qinds = sorter[np.searchsorted(tile_ids,qids,sorter=sorter)]

            #conservative pre-allocation of the arrays we need to populate
            #will truncate at the end
            nmatches = len(matches)
            data = np.zeros(tform_obj.nnz_per_row*tform_obj.rows_per_ptmatch*matrix_assembly['npts_max']*nmatches).astype('float64')
            indices = np.zeros(tform_obj.nnz_per_row*tform_obj.rows_per_ptmatch*matrix_assembly['npts_max']*nmatches).astype('int64')
            indptr = np.zeros(tform_obj.rows_per_ptmatch*matrix_assembly['npts_max']*nmatches+1).astype('int64')
            weights = np.zeros(tform_obj.rows_per_ptmatch*matrix_assembly['npts_max']*nmatches).astype('float64')

            #see definition of CSR format, wikipedia for example
            indptr[0] = 0

            #track how many rows
            nrows = 0

            tilepair_weightfac = tilepair_weight(i,j,matrix_assembly)

            for k in np.arange(nmatches):
                #create the CSR sub-matrix for this tile pair
                d,ind,iptr,wts = tform_obj.CSR_tile_pair(matches[k],pinds[k],qinds[k])
                npts = tform_obj.npts
                if d is None:
                    continue #if npts<nmin, for example

                #add both tile ids to the list
                tiles_used.append(matches[k]['pId'])
                tiles_used.append(matches[k]['qId'])

                #add sub-matrix to global matrix
                global_dind = np.arange(npts*tform_obj.rows_per_ptmatch*tform_obj.nnz_per_row)+nrows*tform_obj.nnz_per_row
                data[global_dind] = d
                indices[global_dind] = ind

                global_rowind = np.arange(npts*tform_obj.rows_per_ptmatch)+nrows
                weights[global_rowind] = wts*tilepair_weightfac
                indptr[global_rowind+1] = iptr+indptr[nrows]

                nrows += wts.size
           
            del matches 
            #truncate, because we allocated conservatively
            data = data[0:nrows*tform_obj.nnz_per_row]
            indices = indices[0:nrows*tform_obj.nnz_per_row]
            indptr = indptr[0:nrows+1]
            weights = weights[0:nrows]

            #can check CSR form here, but seems to be working
            #c = csr_matrix((data,indices,indptr))
            #print '  created submatrix in %0.1f sec.'%(time.time()-t0),'canonical format: ',c.has_canonical_format,', shape: ',c.shape,' nnz: ',c.nnz
            #del c
        
            if file_chunks==0:
                file_data = copy.deepcopy(data)
                file_weights = copy.deepcopy(weights)
                file_indices = copy.deepcopy(indices)
                file_indptr = copy.deepcopy(indptr)
            else:
                file_data = np.append(file_data,data)
                file_weights = np.append(file_weights,weights)
                file_indices = np.append(file_indices,indices)
                lastptr = file_indptr[-1]
                file_indptr = np.append(file_indptr,indptr[1:]+lastptr)
            file_chunks += 1
            file_zlist.append(zvals[i])
            del data,indices,indptr,weights

        if (np.mod(i+1,nmod)==0)|(i==len(zvals)-1):
            c = csr_matrix((file_data,file_indices,file_indptr))
            if output_options['output_mode']=='hdf5':
                if file_number==0:
                    indptr_offset=0
                    vec_offsets = np.array([0,0,0,0]).astype('int64') #different files will keep track of where they are globally
                fname = output_options['output_dir']+'/%d_%d.h5'%(file_zlist[0],file_zlist[-1])
                vec_offsets = write_chunk_to_file(fname,file_number,c,indptr_offset,file_weights,vec_offsets)
                #indptr_offset += file_indptr[-1]
                del file_data,file_indices,file_indptr
                file_chunks = 0
                file_number += 1
                file_zlist = []
 
    outw = sparse.eye(file_weights.size,format='csr')
    outw.data = file_weights
    del file_weights 
    return c,outw,np.unique(np.array(tiles_used))

def create_regularization(regularization,tform_obj,tile_tforms,output_options):
    #affine (half-size) or any other transform, we only need the first one:
    tile_tforms = tile_tforms[0]

    #create a regularization vector
    reg = np.ones_like(tile_tforms).astype('float64')*regularization['default_lambda']
    if 'affine' in tform_obj.name:
        reg[2::3] = reg[2::3]*regularization['translation_factor']
    elif tform_obj.name=='rigid':
        reg[2::4] = reg[2::4]*regularization['translation_factor']
        reg[3::4] = reg[3::4]*regularization['translation_factor']
    if regularization['freeze_first_tile']:
        reg[0:tform_obj.DOF_per_tile] = 1e15

    outr = sparse.eye(reg.size,format='csr')
    outr.data = reg
    return outr

def write_reg_and_tforms(output_options,filt_tforms,reg,filt_tids):
    if output_options['output_mode']=='hdf5':
        #write the input transforms to disk
        fname = output_options['output_dir']+'/regularization.h5'
        f = h5py.File(fname,"w")
        for j in np.arange(len(filt_tforms)):
            dsetname = 'transforms_%d'%j
            dset = f.create_dataset(dsetname,(filt_tforms[j].size,),dtype='float64')
            dset[:] = filt_tforms[j]
        #create a regularization vector
        vec = reg.diagonal()
        dset = f.create_dataset("lambda",(vec.size,),dtype='float64')
        dset[:] = vec
        #keep track here what tile_ids were used
        dt = h5py.special_dtype(vlen=str)
        dset = f.create_dataset("tile_ids",(filt_tids.size,),dtype=dt)
        dset[:] = filt_tids
        f.close()
        print 'wrote %s'%fname

def assemble(mod):
    #make a transform object
    tform_obj = transform_csr(mod.args['transformation'],mod.args['matrix_assembly']['npts_min'],mod.args['matrix_assembly']['npts_max'])

    #get the tile IDs and transforms
    tile_ids,tile_tforms,tile_tspecs,shared_tforms = get_tileids_and_tforms(mod.args['input_stack'],tform_obj,zvals)
    if mod.args['transformation']=='affine':
        #split the tforms in half by u and v
        utforms = np.hstack(np.hsplit(tile_tforms,len(tile_tforms)/3)[::2])
        vtforms = np.hstack(np.hsplit(tile_tforms,len(tile_tforms)/3)[1::2])
        tile_tforms = [utforms,vtforms]
        del utforms,vtforms
    else:
        tile_tforms = [tile_tforms]

    #create A matrix in compressed sparse row (CSR) format
    A,weights,tiles_used = create_CSR_A(mod.args['pointmatch'],mod.args['matrix_assembly'],tform_obj,tile_ids,zvals,mod.args['output_options'])

    #some book-keeping if there were some unused tiles
    tile_ind = np.in1d(tile_ids,tiles_used)
    filt_tspecs = tile_tspecs[tile_ind]
    filt_tids = tile_ids[tile_ind]
    filt_tforms = []
    for j in np.arange(len(tile_tforms)):
        filt_tforms.append(tile_tforms[j][np.repeat(tile_ind,tform_obj.DOF_per_tile/len(tile_tforms))])
    del tile_ids,tiles_used,tile_tforms,tile_ind,tile_tspecs
    
    #create the regularization vectors
    reg = create_regularization(mod.args['regularization'],tform_obj,filt_tforms,mod.args['output_options'])

    #output the regularization vectors to hdf5 file
    write_reg_and_tforms(mod.args['output_options'],filt_tforms,reg,filt_tids)

    return A,weights,reg,filt_tspecs,filt_tforms,filt_tids,shared_tforms

def start_from_file(mod):
    #make a transform object
    tform_obj = transform_csr(mod.args['transformation'],mod.args['matrix_assembly']['npts_min'],mod.args['matrix_assembly']['npts_max'])
    #get the tile IDs and transforms
    tile_ids,tile_tforms,tile_tspecs,shared_tforms = get_tileids_and_tforms(mod.args['input_stack'],tform_obj,zvals)
    
    tmp = mod.args['start_from_file'].split('/')
    fdir = ''
    for t in tmp[:-1]:
        fdir=fdir+'/'+t
    
    #get from the regularization file
    f = h5py.File(fdir+'/regularization.h5','r')
    reg = f.get('lambda')[()]
    filt_tids = f.get('tile_ids')[()]
    k=0
    filt_tforms=[]
    while True:
        name = 'transforms_%d'%k 
        if name in f.keys():
            filt_tforms.append(f.get(name)[()])
            k+=1
        else:
           break
    tile_ind = np.in1d(tile_ids,filt_tids)
    filt_tspecs = tile_tspecs[tile_ind]
    f.close()

    outr = sparse.eye(reg.size,format='csr')
    outr.data = reg
    reg = outr

    #get from the matrix files
    f = open(fdir+'/index.txt','r')
    lines = f.readlines()
    f.close()
    data = np.array([]).astype('float64')
    weights = np.array([]).astype('float64')
    indices = np.array([]).astype('int64')
    indptr = np.array([]).astype('int64')
    for i in np.arange(len(lines)):
        line = lines [i]
        fname = fdir+'/'+line.split(' ')[1]
        f = h5py.File(fname,'r')
        data = np.append(data,f.get('data')[()])
        indices = np.append(indices,f.get('indices')[()])
        if i==0:
            indptr = np.append(indptr,f.get('indptr')[()])
        else:
            indptr = np.append(indptr,f.get('indptr')[()][1:]+indptr[-1])
        weights = np.append(weights,f.get('weights')[()])
        f.close()

    A=csr_matrix((data,indices,indptr))

    outw = sparse.eye(weights.size,format='csr')
    outw.data = weights
    weights = outw

    print 'read inputs from files'

    return A,weights,reg,filt_tspecs,filt_tforms,filt_tids,shared_tforms

def mat_stats(m,name):
    print ' matrix %s: '%name+' format: ',m.getformat(),', shape: ',m.shape,' nnz: ',m.nnz
    if m.shape[0]==m.shape[1]:
        asymm = np.any(m.transpose().data != m.data)
        print ' symm: ',not asymm

def write_to_new_stack(name,tform_type,tspecs,shared_tforms,x,ingestconn):
    #replace the last transform in the tilespec with the new one
    for m in np.arange(len(tspecs)):
        if 'affine' in tform_type:
            tspecs[m].tforms[-1].M[0,0] = x[m*6+0]
            tspecs[m].tforms[-1].M[0,1] = x[m*6+1]
            tspecs[m].tforms[-1].M[0,2] = x[m*6+2]
            tspecs[m].tforms[-1].M[1,0] = x[m*6+3]
            tspecs[m].tforms[-1].M[1,1] = x[m*6+4]
            tspecs[m].tforms[-1].M[1,2] = x[m*6+5]
        elif tform_type=='rigid':
            tspecs[m].tforms[-1].M[0,0] = x[m*4+0]
            tspecs[m].tforms[-1].M[0,1] = x[m*4+1]
            tspecs[m].tforms[-1].M[0,2] = x[m*4+2]
            tspecs[m].tforms[-1].M[1,0] = -x[m*4+1]
            tspecs[m].tforms[-1].M[1,1] = x[m*4+0]
            tspecs[m].tforms[-1].M[1,2] = x[m*4+3]
    tspecs = tspecs.tolist()
    renderapi.client.import_tilespecs_parallel(name,tspecs,sharedTransforms=shared_tforms,render=ingestconn,close_stack=False)
    
def solve_or_not(mod,A,weights,reg,filt_tforms):
    t0=time.time()
    #not
    if mod.args['output_options']['output_mode'] in ['hdf5']:
        message = '*****\nno solve for file output\n'
        message += 'solve from the files you just wrote:\n\n'
        message += 'python '
        for arg in sys.argv:
            message += arg+' '
        message = message+ '--start_from_file '+mod.args['output_options']['output_dir']
        message += '\n\nor, run it again to solve with no output:\n\n'
        message += 'python '
        for arg in sys.argv:
            message += arg+' '
        message = message.replace('hdf5','none')
        x=None
    else:
        #regularized least squares
        ATW = A.transpose().dot(weights)
        K = ATW.dot(A) + reg
        mat_stats(K,'K')
        print ' K created in %0.1f seconds'%(time.time()-t0)
        t0=time.time()
        del weights,ATW
    
        #factorize, then solve, efficient for large affine
        solve = factorized(K)
        if mod.args['transformation']=='affine':
            #affine assembles only half the matrix
            #then applies the LU decomposition to the u and v transforms separately
            Lm = reg.dot(filt_tforms[0])
            xu = solve(Lm)
            erru = A.dot(xu)
            precisionu = np.linalg.norm(K.dot(xu)-Lm)/np.linalg.norm(Lm)

            Lm = reg.dot(filt_tforms[1])
            xv = solve(Lm)
            errv = A.dot(xv)
            precisionv = np.linalg.norm(K.dot(xv)-Lm)/np.linalg.norm(Lm)
            precision = np.sqrt(precisionu**2+precisionv**2)
           
            #recombine
            x = np.zeros(xu.size*2).astype('float64')
            err = np.hstack((erru,errv))
            for i in np.arange(3):
                x[i::6]=xu[i::3]
                x[i+3::6]=xv[i::3]
            del xu,xv,erru,errv,precisionu,precisionv
        else:
            #simpler case for rigid, or affine_fullsize, but 2x larger than affine
            Lm = reg.dot(filt_tforms[0])
            x = solve(Lm)
            err = A.dot(x)
            precision = np.linalg.norm(K.dot(x)-Lm)/np.linalg.norm(Lm)
        del K,Lm

        message = '***-------------***\n'
        message = message + ' solved in %0.1f sec\n'%(time.time()-t0)
        message = message + ' precision [norm(Kx-Lm)/norm(Lm)] = %0.1e\n'%precision
        message = message + ' error     [norm(Ax-b)] = %0.3f\n'%(np.linalg.norm(err))
        message = message + ' avg cartesian projection displacement per point [mean(|Ax|)+/-std(|Ax|)] : %0.1f +/- %0.1f pixels'%(np.abs(err).mean(),np.abs(err).std())

    return message,x

def assemble_and_solve(mod,zvals,ingestconn):
    t0 = time.time()
    t00 = t0
    #assembly
    if mod.args['start_from_file']!='':
        A,weights,reg,filt_tspecs,filt_tforms,filt_tids,shared_tforms = start_from_file(mod)
    else:
        A,weights,reg,filt_tspecs,filt_tforms,filt_tids,shared_tforms = assemble(mod)
    mat_stats(A,'A')
    print ' A created in %0.1f seconds'%(time.time()-t0)

    #solve
    message,x = solve_or_not(mod,A,weights,reg,filt_tforms)
    print message
    del A

    if mod.args['output_options']['output_mode']=='stack':
        write_to_new_stack(mod.args['output_stack']['name'],mod.args['transformation'],filt_tspecs,shared_tforms,x,ingestconn)
        print message
    del shared_tforms,x,filt_tspecs
    
if __name__=='__main__':
    t0 = time.time()
    mod = argschema.ArgSchemaParser(schema_type=EMA_Schema)

    #specify the z values
    zvals = np.arange(mod.args['first_section'],mod.args['last_section']+1)

    ingestconn=None
    #make a connection to the new stack
    if mod.args['output_options']['output_mode']=='stack':
        ingestconn = make_dbconnection(mod.args['output_stack'])
        renderapi.stack.create_stack(mod.args['output_stack']['name'],render=ingestconn)

    #montage
    if mod.args['solve_type']=='montage':
        for z in zvals:
            assemble_and_solve(mod,[z],ingestconn)
    #3D
    elif mod.args['solve_type']=='3D':
        assemble_and_solve(mod,zvals,ingestconn)
     
    if ingestconn!=None:
        if mod.args['close_stack']:
            renderapi.stack.set_stack_state(mod.args['output_stack']['name'],state='COMPLETE',render=ingestconn)

    print 'total time: %0.1f'%(time.time()-t0)

