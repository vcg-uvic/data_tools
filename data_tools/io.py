import numpy as np
import threading
import multiprocessing
import time


class data_flow(object):
    """
    Given a list of array-like objects, data from the objects is read in a
    parallel thread and processed in the same parallel thread or in a set of
    parallel processes. All objects are iterated in tandem (i.e. for a list
    data=[A, B, C], a batch of size 1 would be [A[i], B[i], C[i]] for some i).
    
    data : A list of data arrays, each of equal length. When yielding a batch, 
        each element of the batch corresponds to each array in the data list.
    batch_size : The maximum number of elements to yield from each data array
        in a batch. The actual batch size is the smallest of either this number
        or the number of elements not yet yielded in the current epoch.
    nb_io_workers : The number of parallel threads to preload data. NOTE that
        if nb_io_workers > 1, data is loaded asynchronously.
    nb_proc_workers : The number of parallel processes to do preprocessing of
        data using the _process_batch function. If nb_proc_workers is set to 0,
        no parallel processes will be launched; instead, any preprocessing will
        be done in the preload thread and data will have to pass through only
        one queue rather than two queues. NOTE that if nb_proc_workers > 1,
        data processing is asynchronous and data will not be yielded in the
        order that it is loaded!
    shuffle : If True, access the elements of the data arrays in random 
        order.
    loop_forever : If False, stop iteration at the end of an epoch (when all
        data has been yielded once).
    preprocessor : The preprocessor function to call on a batch. As input,
        takes a batch of the same arrangement as `data`.
    rng : A numpy random number generator. The rng is used to determine data
        shuffle order and is used to uniquely seed the numpy RandomState in
        each parallel process (if any).
    """
    
    def __init__(self, data, batch_size, nb_io_workers=1, nb_proc_workers=0,
                 shuffle=False, loop_forever=True, preprocessor=None,
                 rng=None):
        self.data = data
        self.batch_size = batch_size
        self.nb_io_workers = nb_io_workers
        self.nb_proc_workers = nb_proc_workers
        self.shuffle = shuffle
        self.loop_forever = loop_forever
        if preprocessor is not None:
            self._process_batch = preprocessor
        else:
            self._process_batch = lambda x: x[0]   # Do nothing by default
        if rng is None:
            self.rng = np.random.RandomState()
        else:
            self.rng = rng
        
        self.num_samples = len(data[0])
        for d in self.data:
            assert(len(d)==self.num_samples)
        
        self.num_batches = self.num_samples//self.batch_size
        if self.num_samples%self.batch_size > 0:
            self.num_batches += 1
        
    ''' Generate batches of processed data (output with labels) '''
    def flow(self):
        # Create the generator that loads data (shared by all loading threads)
        def load_generator():
            if self.shuffle:
                indices = self.rng.permutation(self.num_samples)
            else:
                indices = np.arange(self.num_samples)
            while 1:
                for b in range(self.num_batches):
                    bs = self.batch_size
                    batch_indices = indices[b*bs:(b+1)*bs]
                    batch = []
                    for d in self.data:
                        batch.append([np.array(d[int(i)]) \
                                               for i in batch_indices])
                    yield batch
                if not self.loop_forever:
                    break
        self._load_generator = load_generator()
        
        # Create a stop event to trigger on exceptions/interrupt/termination.
        stop = threading.Event()
        
        # Prepare to start processes + thread.
        load_queue = None
        load_queue_semaphore = None
        proc_queue = None
        process_list = []
        preload_thread = None
        try:
            # Create the queues.
            #   NOTE: these can become corrupt on sub-process termination,
            #   so create them in flow() and let them die with the flow().
            q_size = max(self.nb_io_workers, self.nb_proc_workers)
            load_queue = multiprocessing.Queue(q_size)
            load_queue_semaphore = multiprocessing.Semaphore(q_size)
            if self.nb_proc_workers > 0:
                proc_queue = multiprocessing.Queue(q_size)
            else:
                # If there are no worker processes, alias load_queue as
                # proc_queue, allowing data to thus be yielded directly from
                # the load_queue.
                proc_queue = load_queue
            
            # Start the parallel data processing proccess(es)
            seed_base = self.rng.randint(self.nb_proc_workers, 2**16)
            for i in range(self.nb_proc_workers):
                pseed = seed_base - i
                process_thread = multiprocessing.Process( \
                    target=self._process_subroutine,
                    args=(load_queue, proc_queue, stop, pseed))
                process_thread.daemon = True
                process_thread.start()
                process_list.append(process_thread)
                
            # Start the parallel loader thread.
            # (must be started AFTER processes to avoid copying it in fork())
            preload_thread = threading.Thread( \
                target=self._preload_subroutine,
                args=(load_queue, load_queue_semaphore, stop) )
            preload_thread.daemon = True
            preload_thread.start()
            
            # Yield batches fetched from the parallel process(es).
            samples_yielded = 0
            nb_yielded = 0
            while not stop.is_set():
                try:
                    if not self.loop_forever and nb_yielded==self.num_batches:
                        stop.set()
                        continue
                    batch = proc_queue.get()
                    #print("DEBUG IO: ", np.unique(batch[1]))
                    yield batch
                    nb_yielded += 1
                    samples_yielded += len(batch[0])
                except:
                    stop.set()
                    raise
        except:
            stop.set()
            raise
        finally:
            # Clean up, whether there was an exception or not.
            #
            # Set termination event, terminate all processes, close queues, and
            # wait for loading thread to end.
            stop.set()
            if self.nb_proc_workers and proc_queue is not None:
                # If nb_proc_workers==0, proc_queue is just an alias to
                # load_queue
                proc_queue.close()
            if load_queue is not None:
                load_queue.cancel_join_thread()
                load_queue.close()
            for process in process_list:
                if process.is_alive():
                    process.terminate()
            if preload_thread is not None:
                preload_thread.join()
            
    ''' Preload batches in the background and add them into the load_queue.
        Wait if the queue is full. '''
    def _preload_subroutine(self, load_queue, semaphore, stop):
        try:
            while not stop.is_set():
                while load_queue.full():
                    time.sleep(0.001)
                    if stop.is_set(): return
                try:
                    batch = next(self._load_generator)
                except StopIteration:
                    if self.loop_forever: raise
                    break
                while not semaphore.acquire(timeout=0.001):
                    # Poll here in case more than one thread attempts to
                    # acquire the semaphore while there is only one spot
                    # left in the load_queue.
                    if stop.is_set(): return
                if self.nb_proc_workers > 0:
                    # If there are worker processes, they will preprocess.
                    load_queue.put( batch )
                else:
                    # If there are no worker processes, preprocess
                    # the batch in the loader thread.
                    load_queue.put( self._process_batch(batch) )
                semaphore.release()
        except:
            stop.set()
            raise
                
    ''' Process any loaded batches in the load queue and add them to the
        processed queue -- these are ready to yield. '''
    def _process_subroutine(self, load_queue, proc_queue, stop, seed):
        np.random.seed(seed)
        while not stop.is_set():
            try:
                batch = load_queue.get()
                proc_queue.put(self._process_batch(batch))
            except:
                stop.set()
                raise
            
    def __len__(self):
        return self.num_batches



class buffered_array_writer(object):
    """
    Given an array, data element shape, and batch size, writes data to an array
    batch-wise. Data can be passed in any number of elements at a time.
    If the array is an interface to a memory-mapped file, data is thus written
    batch-wise to the file.
    
    INPUTS
    storage_array      : the array to write into
    data_element_shape : shape of one input element
    batch_size         : write the data to disk in batches of this size
    length             : dataset length (if None, expand it dynamically)
    """
    
    def __init__(self, storage_array, data_element_shape, dtype, batch_size,
                 length=None):
        self.storage_array = storage_array
        self.data_element_shape = data_element_shape
        self.dtype = dtype
        self.batch_size = batch_size
        self.length = length
        
        self.buffer = np.zeros((batch_size,)+data_element_shape, dtype=dtype)
        self.buffer_ptr = 0
        self.storage_array_ptr = 0
        
    ''' Flush the buffer. '''
    def flush_buffer(self):
        if self.buffer_ptr > 0:
            end = self.storage_array_ptr+self.buffer_ptr
            self.storage_array[self.storage_array_ptr:end] = \
                                                  self.buffer[:self.buffer_ptr]
            self.storage_array_ptr += self.buffer_ptr
            self.buffer_ptr = 0
            
    '''
    Write data to file one buffer-full at a time. Note: data is not written
    until buffer is full.
    '''
    def buffered_write(self, data):
        # Verify data shape 
        if np.shape(data) != self.data_element_shape \
                             and np.shape(data)[1:] != self.data_element_shape:
            raise ValueError("Error: input data has the wrong shape.")
        if np.shape(data) == self.data_element_shape:
            data_len = 1
        elif np.shape(data)[1:] == self.data_element_shape:
            data_len = len(data)
            
        # Stop when data length exceeded
        if self.length is not None and self.length==self.storage_array_ptr:
            raise EOFError("Write aborted: length of input data exceeds "
                           "remaining space.")
            
        # Verify data type
        if data.dtype != self.dtype:
            raise TypeError
            
        # Buffer/write
        if data_len == 1:
            data = [data]
        for d in data:
            self.buffer[self.buffer_ptr] = d
            self.buffer_ptr += 1
            
            # Flush buffer when full
            if self.buffer_ptr==self.batch_size:
                self.flush_buffer()
                
        # Flush the buffer when 'length' reached
        if self.length is not None \
                       and self.storage_array_ptr+self.buffer_ptr==self.length:
            self.flush_buffer()
            
    def __len__(self):
        num_elements = len(self.storage_array)+self.buffer_ptr
        return num_elements
            
    def get_shape(self):
        return (len(self),)+self.data_element_shape
    
    def get_element_shape(self):
        return self.data_element_shape
    
    def get_array(self):
        return self.storage_array
        
    def __del__(self):
        self.flush_buffer()


class h5py_array_writer(buffered_array_writer):
    """
    Given a data element shape and batch size, writes data to an HDF5 file
    batch-wise. Data can be passed in any number of elements at a time.
    
    INPUTS
    data_element_shape : shape of one input element
    batch_size         : write the data to disk in batches of this size
    filename           : name of file in which to store data
    array_name         : HDF5 array path
    length             : dataset length (if None, expand it dynamically)
    append             : write files with append mode instead of write mode
    kwargs             : dictionary of arguments to pass to h5py on dataset
                         creation (if none, do lzf compression with
                         batch_size chunk size)
    """
    
    def __init__(self, data_element_shape, dtype, batch_size, filename,
                 array_name, length=None, append=False, kwargs=None):
        import h5py
        super(h5py_array_writer, self).__init__(None, data_element_shape,
                                                dtype, batch_size, length)
        self.filename = filename
        self.array_name = array_name
        self.kwargs = kwargs
        
        # Set up array kwargs
        self.arr_kwargs = {'chunks': (batch_size,)+data_element_shape,
                           'compression': 'lzf',
                           'dtype': dtype}
        if kwargs is not None:
            self.arr_kwargs.update(kwargs)
    
        # Open the file for writing.
        self.file = None
        if append:
            self.write_mode = 'a'
        else:
            self.write_mode = 'w'
        try:
            self.file = h5py.File(filename, self.write_mode)
        except:
            print("Error: failed to open file %s" % filename)
            raise
        
        # Open an array interface (check if the array exists; if not, create it)
        if self.length is None:
            ds_args = (self.array_name, (1,)+self.data_element_shape)
        else:
            ds_args = (self.array_name, (self.length,)+self.data_element_shape)
        try:
            self.storage_array = self.file[self.array_name]
            self.storage_array_ptr = len(self.storage_array)
        except KeyError:
            self.storage_array = self.file.create_dataset( *ds_args,
                               dtype=self.dtype,
                               maxshape=(self.length,)+self.data_element_shape,
                               **self.arr_kwargs )
            self.storage_array_ptr = 0
            
    ''' Flush the buffer. Resize the dataset, if needed. '''
    def flush_buffer(self):
        if self.buffer_ptr > 0:
            end = self.storage_array_ptr+self.buffer_ptr
            if self.length is None:
                self.storage_array.resize( (end,)+self.data_element_shape )
            self.storage_array[self.storage_array_ptr:end] = \
                                                  self.buffer[:self.buffer_ptr]
            self.storage_array_ptr += self.buffer_ptr
            self.buffer_ptr = 0
    
    ''' Flush remaining data in the buffer to file and close the file. '''
    def __del__(self):
        self.flush_buffer()
        if self.file is not None:
            self.file.close() 


class bcolz_array_writer(buffered_array_writer):
    """
    Given a data element shape and batch size, writes data to a bcolz file-set
    batch-wise. Data can be passed in any number of elements at a time.
    
    INPUTS
    data_element_shape : shape of one input element
    batch_size         : write the data to disk in batches of this size
    save_path          : directory to save array in
    length             : dataset length (if None, expand it dynamically)
    append             : write files with append mode instead of write mode
    kwargs             : dictionary of arguments to pass to bcolz on dataset 
                         creation (if none, do blosc compression with chunklen
                         determined by the expected array length)
    """
    
    def __init__(self, data_element_shape, dtype, batch_size, save_path,
                 length=None, append=False, kwargs={}):
        import bcolz
        super(bcolz_array_writer, self).__init__(None, data_element_shape,
                                                 dtype, batch_size, length)
        self.save_path = save_path
        self.kwargs = kwargs
        
        # Set up array kwargs
        self.arr_kwargs = {'expectedlen': length,
                           'cparams': bcolz.cparams(clevel=5,
                                                    shuffle=True,
                                                    cname='blosclz'),
                           'dtype': dtype,
                           'rootdir': save_path}
        if kwargs is not None:
            self.arr_kwargs.update(kwargs)
    
        # Create the file-backed array, open for writing.
        # (check if the array exists; if not, create it)
        if append:
            try:
                self.storage = bcolz.open(self.save_path, mode='a')
                self.storage_array_ptr = len(self.storage_array)
            except FileNotFoundError:
                append=False
        if not append:
            try:
                self.storage_array = bcolz.zeros(shape=(0,)+data_element_shape,
                                                 dtype=np.float32,
                                                 rootdir=self.save_path,
                                                 mode=self.write_mode,
                                                 **self.arr_kwargs )
                self.storage_array_ptr = 0
            except:
                print("Error: failed to create file-backed bcolz storage "
                      "array.")
                raise
            
    ''' Flush the buffer. '''
    def flush_buffer(self):
        if self.buffer_ptr > 0:
            self.storage_array.append(self.buffer[:self.buffer_ptr])
            self.storage_array.flush()
            self.storage_array_ptr += self.buffer_ptr
            self.buffer_ptr = 0


class zarr_array_writer(buffered_array_writer):
    """
    Given a data element shape and batch size, writes data to a zarr file
    batch-wise. Data can be passed in any number of elements at a time.
    
    INPUTS
    data_element_shape : shape of one input element
    batch_size         : write the data to disk in batches of this size
    filename           : name of file in which to store data
    array_name         : zarr array path
    length             : dataset length (if None, expand it dynamically)
    append             : write files with append mode instead of write mode
    kwargs             : dictionary of arguments to pass to zarr on dataset
                         creation (if none, do blosc lz4 compression with
                         batch_size chunk size)
    """
    
    def __init__(self, data_element_shape, dtype, batch_size, filename,
                 array_name, length=None, append=False, kwargs=None):
        import zarr
        super(zarr_array_writer, self).__init__(None, data_element_shape,
                                                dtype, batch_size, length)
        self.filename = filename
        self.array_name = array_name
        self.kwargs = kwargs
        
        # Set up array kwargs
        self.arr_kwargs = {'name': array_name,
                           'chunks': (batch_size,)+data_element_shape,
                           'compressor': zarr.Blosc(cname='lz4',
                                                    clevel=5,
                                                    shuffle=1),
                           'dtype': dtype}
        if self.length is None:
            self.arr_kwargs['shape'] = (1,)+self.data_element_shape
        else:
            self.arr_kwargs['shape'] = (self.length,)+self.data_element_shape
        if kwargs is not None:
            self.arr_kwargs.update(kwargs)
    
        # Open the file for writing.
        self.group = None
        if append:
            self.write_mode = 'a'
        else:
            self.write_mode = 'w'
        try:
            self.group = zarr.open_group(filename, self.write_mode)
        except:
            print("Error: failed to open file %s" % filename)
            raise
        
        # Open an array interface (check if the array exists; if not, create it)
        if self.length is None:
            ds_args = (self.array_name, (1,)+self.data_element_shape)
        else:
            ds_args = (self.array_name, (self.length,)+self.data_element_shape)
        try:
            self.storage_array = self.group[self.array_name]
            self.storage_array_ptr = len(self.storage_array)
        except KeyError:
            self.storage_array = self.group.create_dataset(**self.arr_kwargs)
            self.storage_array_ptr = 0
            
    ''' Flush the buffer. Resize the dataset, if needed. '''
    def flush_buffer(self):
        if self.buffer_ptr > 0:
            end = self.storage_array_ptr+self.buffer_ptr
            if self.length is None:
                self.storage_array.resize( (end,)+self.data_element_shape )
            self.storage_array[self.storage_array_ptr:end] = \
                                                  self.buffer[:self.buffer_ptr]
            self.storage_array_ptr += self.buffer_ptr
            self.buffer_ptr = 0
    
    ''' Flush remaining data in the buffer to file and close the file. '''
    def __del__(self):
        self.flush_buffer()
        # Zarr automatically flushes all modifications and does not expose
        # the file handle so the file is not closed in this destructor.