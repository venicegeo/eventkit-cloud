import { mount } from 'enzyme';
import CircularProgress from '@material-ui/core/CircularProgress';
import Loading from '../../components/auth/Loading';

describe('Loading component', () => {
    it('should render basic elemets', () => {
        const wrapper = mount(Loading());
        expect(wrapper.find(CircularProgress)).toHaveLength(1);
    });
});
